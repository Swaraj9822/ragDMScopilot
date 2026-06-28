"""PostgreSQL-backed trace store for the AI observability platform.

:class:`PostgresTraceStore` is the durable persistence layer for traces and
their spans. It reuses the schema and ``COPILOT_DB_*`` connection settings from
:mod:`rag_system.observability_tracing.schema`, the psycopg connection style of
:class:`rag_system.copilot.PostgresCopilotExecutor`, and the
:class:`~rag_system.observability_tracing.serializer.TraceSerializer` to convert
an in-memory :class:`Trace` into its stored (JSONB attribute) representation.

This module implements task 9.1 (:meth:`PostgresTraceStore.persist`), task 9.2
(:meth:`get_trace`, :meth:`search_traces`), and task 9.3
(:meth:`enforce_retention`).

Requirements covered by ``persist``:

* R5.1 / R5.5 — the trace row and every span row are written inside a single
  atomic transaction; any failure rolls the whole transaction back so neither
  the trace nor any of its spans remain.
* R5.4 / R10.3 — on failure the store logs at WARNING (wrapped so a failing
  warning call cannot itself raise), discards the trace, increments
  ``rag_trace_store_write_failures_total``, and never raises to the caller.
* R5.6 — ``rag_traces_persisted_total{route=...}`` is incremented by exactly one
  only after the transaction has fully committed.

Testability
-----------
The store accepts an injected ``connection_factory`` and ``metrics`` so the
atomic-transaction behaviour can be exercised without a live database. The
factory is any zero-argument callable returning a psycopg-style connection that
works as a context manager: committing (and closing) on a clean exit and rolling
back on an exception, exactly like ``psycopg.connect(...)``. When no factory is
supplied, the store connects using the ``COPILOT_DB_*`` settings via
:func:`rag_system.observability_tracing.schema.connect`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from rag_system.config import Settings
from rag_system.observability import get_logger
from rag_system.observability import metrics as _default_metrics

from . import schema
from .models import AttributeValue, Span, Trace
from .serializer import TraceSerializer

if TYPE_CHECKING:  # pragma: no cover - typing only
    import logging
    from contextlib import AbstractContextManager

    from .models import StoredTrace

logger = get_logger(__name__)

__all__ = [
    "DELETE_EXPIRED_TRACES_SQL",
    "INSERT_SPAN_SQL",
    "INSERT_TRACE_SQL",
    "PostgresTraceStore",
    "TraceSearchFilters",
]

#: Metric incremented once per trace whose transaction fully commits (R5.6).
_PERSISTED_METRIC = "rag_traces_persisted_total"

#: Metric incremented once per failed trace-store write (R5.4, R10.3).
_WRITE_FAILURE_METRIC = "rag_trace_store_write_failures_total"

#: Metric incremented once per trace whose retention deletion failed (R13.5).
_RETENTION_FAILURE_METRIC = "rag_trace_store_retention_failures_total"

#: Default and maximum result limits for :meth:`PostgresTraceStore.search_traces`
#: (R8.6 default 100, R8.7 capped at 1000).
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


# ---------------------------------------------------------------------------
# Insert statements (one transaction inserts the trace row + all span rows)
# ---------------------------------------------------------------------------

INSERT_TRACE_SQL = """
    INSERT INTO traces (trace_id, route, start_ts, duration_ms, root_status)
    VALUES (%s, %s, %s::timestamptz, %s, %s)
"""
"""Insert a single ``traces`` row (R5.2)."""

INSERT_SPAN_SQL = """
    INSERT INTO spans (
        trace_id, span_id, parent_span_id, operation,
        start_ts, duration_ms, status, attributes
    )
    VALUES (%s, %s, %s, %s, %s::timestamptz, %s, %s, %s::jsonb)
"""
"""Insert a single ``spans`` row; ``attributes`` is passed as a JSON string and
cast to ``jsonb`` so the store does not depend on a psycopg JSON adapter (R5.3)."""


# ---------------------------------------------------------------------------
# Select statements (consumed by get_trace / search_traces — task 9.2)
# ---------------------------------------------------------------------------

#: Trace-row columns selected by the read queries, in :meth:`_row_to_trace` order.
_TRACE_COLUMNS = "trace_id, route, start_ts, duration_ms, root_status"

#: Span-row columns selected by the read queries, in :meth:`_row_to_span` order.
_SPAN_COLUMNS = "span_id, parent_span_id, operation, start_ts, duration_ms, status, attributes"

SELECT_TRACE_SQL = f"""
    SELECT {_TRACE_COLUMNS}
    FROM traces
    WHERE trace_id = %s
"""
"""Fetch a single ``traces`` row by id (R7.1); zero rows means not found (R7.4)."""

SELECT_SPANS_SQL = f"""
    SELECT {_SPAN_COLUMNS}
    FROM spans
    WHERE trace_id = %s
    ORDER BY start_ts ASC, span_id ASC
"""
"""All spans for a trace, ordered by start timestamp then span_id, both ascending
(R7.2); the Root_Span's ``parent_span_id`` is null (R7.5)."""


# ---------------------------------------------------------------------------
# Retention statements (task 9.3 — R13)
# ---------------------------------------------------------------------------

DELETE_EXPIRED_TRACES_SQL = """
    DELETE FROM traces
    WHERE start_ts < %s::timestamptz
"""
"""Bulk-delete every trace strictly older than the cutoff (``start_ts < cutoff``
⇔ ``now - start_ts > max_age``); traces exactly at the boundary
(``start_ts == cutoff``) are retained (R13.1). Each removed trace's spans are
deleted by the ``spans`` foreign key ``ON DELETE CASCADE`` within the same
transaction (R13.2)."""

SELECT_EXPIRED_TRACE_IDS_SQL = """
    SELECT trace_id FROM traces
    WHERE start_ts < %s::timestamptz
    ORDER BY trace_id
"""
"""Candidate trace ids for per-row retention when the bulk delete fails, so each
removal can be isolated (R13.5)."""

DELETE_TRACE_BY_ID_SQL = """
    DELETE FROM traces
    WHERE trace_id = %s
"""
"""Delete a single trace by id during the per-row retention fallback so a failing
trace does not abort the whole cycle; its spans cascade with it (R13.2, R13.5)."""


# ---------------------------------------------------------------------------
# Search filters (consumed by search_traces — implemented in task 9.2)
# ---------------------------------------------------------------------------


@dataclass
class TraceSearchFilters:
    """Filters for :meth:`PostgresTraceStore.search_traces` (R8).

    Defined here so the query implementation (task 9.2) and its callers share a
    single filter shape; mirrors the test double's ``TraceSearchFilters``.
    """

    start: datetime | None = None            # inclusive lower bound (R8.1)
    end: datetime | None = None              # inclusive upper bound (R8.1)
    route: str | None = None                 # case-sensitive (R8.2)
    status: str | None = None                # case-sensitive (R8.3)
    min_duration_ms: int | None = None       # >= (R8.4)
    limit: int = 100                         # default 100, capped 1000 (R8.6, R8.7)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PostgresTraceStore:
    """Durable, queryable trace persistence backed by PostgreSQL.

    Args:
        settings: Application settings carrying the ``COPILOT_DB_*`` connection
            values, reused by the default connection factory.
        connection_factory: Optional zero-argument callable returning a
            psycopg-style connection usable as a context manager (commit + close
            on clean exit, rollback on exception). Defaults to connecting via
            :func:`schema.connect`. Injecting a factory makes the atomic-write
            behaviour testable without a live database.
        serializer: Optional :class:`TraceSerializer` (Trace -> StoredTrace).
        metrics: Optional metrics registry; defaults to the shared
            :data:`rag_system.observability.metrics` registry.
        logger_: Optional logger; defaults to this module's logger.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        serializer: TraceSerializer | None = None,
        metrics: Any | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._connection_factory = connection_factory or self._default_connection_factory
        self._serializer = serializer or TraceSerializer()
        self._metrics = metrics if metrics is not None else _default_metrics
        self._logger = logger_ or logger
        #: Error indications for traces whose retention deletion failed (R13.5).
        #: Each entry is ``{"trace_id": <id or None>, "error": <str>}``; the most
        #: recent :meth:`enforce_retention` cycle's failures are appended here.
        self.retention_errors: list[dict[str, Any]] = []

    # -- connection ---------------------------------------------------------

    def _default_connection_factory(self) -> AbstractContextManager[Any]:
        """Open a psycopg connection using the ``COPILOT_DB_*`` settings.

        psycopg's connection context manager commits (and closes) on a clean
        exit and rolls back on an exception, giving us the single atomic
        transaction the persist contract requires.
        """
        return schema.connect(self._settings)

    # -- persist (task 9.1) -------------------------------------------------

    def persist(self, trace: Trace) -> None:
        """Persist *trace* and all of its spans in one atomic transaction.

        On success the per-route persisted-traces counter is incremented exactly
        once, only after the transaction has committed (R5.6). On any failure the
        whole transaction is rolled back so no partial trace/span data survives
        (R5.1, R5.5); the failure is logged at WARNING (wrapped so a failing
        warning call cannot itself raise), the trace is discarded, the
        write-failure counter is incremented, and nothing is raised to the
        caller (R5.4, R10.3).
        """
        route = getattr(trace, "route", None)
        try:
            stored = self._serializer.serialize(trace)
            self._write_atomically(stored)
        except Exception as exc:  # noqa: BLE001 - best-effort: never raise to caller
            self._handle_failure(trace, exc)
            return

        # Reached only after the transaction has fully committed (R5.6).
        self._increment(_PERSISTED_METRIC, {"route": str(route)})

    def _write_atomically(self, stored: StoredTrace) -> None:
        """Insert the trace row and every span row inside one transaction.

        Using the connection as a context manager means a clean exit commits the
        transaction and any exception rolls it back in full (R5.1, R5.5).
        """
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    INSERT_TRACE_SQL,
                    (
                        stored["trace_id"],
                        stored["route"],
                        stored["start_ts"],
                        stored["duration_ms"],
                        stored["root_status"],
                    ),
                )
                for span in stored["spans"]:
                    cur.execute(
                        INSERT_SPAN_SQL,
                        (
                            stored["trace_id"],
                            span["span_id"],
                            span["parent_span_id"],
                            span["operation"],
                            span["start_ts"],
                            span["duration_ms"],
                            span["status"],
                            json.dumps(span["attributes"]),
                        ),
                    )

    def _handle_failure(self, trace: Trace, exc: Exception) -> None:
        """Best-effort failure handling: discard the trace and never raise.

        The transaction has already rolled back (no partial rows survive). We log
        at WARNING wrapped in a bare ``try/except`` so that a failure inside the
        warning call itself cannot propagate (R5.4), and increment the
        write-failure counter (R10.3).
        """
        trace_id = getattr(trace, "trace_id", None)
        try:
            self._logger.warning(
                "Failed to persist trace %s; discarding: %s",
                trace_id,
                exc,
                extra={"trace_id": trace_id} if isinstance(trace_id, str) else None,
            )
        except Exception:  # noqa: BLE001 - a failing warning must not propagate (R5.4)
            pass
        self._increment(_WRITE_FAILURE_METRIC, {"route": str(getattr(trace, "route", None))})

    def _increment(self, name: str, labels: dict[str, Any]) -> None:
        """Increment a counter, tolerating a metrics backend that raises."""
        try:
            self._metrics.increment(name, labels)
        except Exception:  # noqa: BLE001 - metrics must never break persistence
            pass

    # -- query / retention (tasks 9.2 and 9.3) ------------------------------

    def get_trace(self, trace_id: str) -> Trace | None:
        """Return the persisted trace with all of its spans, or ``None``.

        The trace and its spans are fetched and rebuilt into a :class:`Trace`.
        Spans are ordered by start timestamp ascending, ties broken by span_id
        ascending (R7.2); the Root_Span carries a null ``parent_span_id`` (R7.5).
        Returns ``None`` when no trace exists for *trace_id* (R7.1, R7.4).
        """
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(SELECT_TRACE_SQL, (trace_id,))
                trace_row = cur.fetchone()
                if trace_row is None:
                    return None
                cur.execute(SELECT_SPANS_SQL, (trace_id,))
                span_rows = cur.fetchall()
        spans = [self._row_to_span(row) for row in span_rows]
        return self._row_to_trace(trace_row, spans)

    def search_traces(self, filters: TraceSearchFilters) -> list[Trace]:
        """Return traces matching every supplied filter (AND semantics).

        Applies an inclusive ``[start, end]`` range on the trace start timestamp
        (R8.1), case-sensitive ``route`` (R8.2) and ``status`` (R8.3) equality,
        and a ``min_duration_ms`` lower bound (R8.4). Filters combine with AND
        semantics (R8.5). Results are ordered by start timestamp descending and
        capped at the effective limit (default 100, max 1000 — R8.6, R8.7). An
        empty list is returned when nothing matches (R8.10). Each returned trace
        includes all of its spans (ordered ascending by start timestamp then
        span_id) for consistency with :meth:`get_trace` and the in-memory store
        double the property tests compare against.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if filters.start is not None:
            clauses.append("start_ts >= %s::timestamptz")
            params.append(_to_iso_utc(filters.start))
        if filters.end is not None:
            clauses.append("start_ts <= %s::timestamptz")
            params.append(_to_iso_utc(filters.end))
        if filters.route is not None:
            clauses.append("route = %s")
            params.append(filters.route)
        if filters.status is not None:
            clauses.append("root_status = %s")
            params.append(filters.status)
        if filters.min_duration_ms is not None:
            clauses.append("duration_ms >= %s")
            params.append(filters.min_duration_ms)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = self._effective_limit(filters.limit)
        sql = (
            f"SELECT {_TRACE_COLUMNS} FROM traces\n"
            f"    {where}\n"
            "    ORDER BY start_ts DESC\n"
            "    LIMIT %s"
        )
        params.append(limit)

        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                trace_rows = cur.fetchall()
                traces: list[Trace] = []
                for trace_row in trace_rows:
                    cur.execute(SELECT_SPANS_SQL, (trace_row[0],))
                    spans = [self._row_to_span(row) for row in cur.fetchall()]
                    traces.append(self._row_to_trace(trace_row, spans))
        return traces

    # -- row reconstruction -------------------------------------------------

    @staticmethod
    def _row_to_trace(row: Any, spans: list[Span]) -> Trace:
        """Rebuild a :class:`Trace` from a ``traces`` row (``_TRACE_COLUMNS`` order).

        ``start_ts`` comes back as a timezone-aware ``datetime`` (or an ISO-8601
        string via a fake connection in tests) and is normalised to UTC.
        """
        trace_id, route, start_ts, duration_ms, root_status = row
        return Trace(
            trace_id=trace_id,
            route=route,
            start_ts=_coerce_utc(start_ts),
            duration_ms=int(duration_ms),
            root_status=root_status,
            spans=spans,
        )

    @staticmethod
    def _row_to_span(row: Any) -> Span:
        """Rebuild a :class:`Span` from a ``spans`` row (``_SPAN_COLUMNS`` order).

        ``start_ts`` is normalised to UTC; ``attributes`` is JSONB returned as a
        mapping by psycopg (or, via a fake connection in tests, possibly a JSON
        string) and is rebuilt into a plain ``dict``.
        """
        span_id, parent_span_id, operation, start_ts, duration_ms, status, attributes = row
        return Span(
            span_id=span_id,
            parent_span_id=parent_span_id,
            operation=operation,
            start_ts=_coerce_utc(start_ts),
            duration_ms=int(duration_ms),
            status=status,
            attributes=_coerce_attributes(attributes),
        )

    @staticmethod
    def _effective_limit(limit: int | None) -> int:
        """Clamp a requested limit to ``[0, 1000]``; ``None`` defaults to 100."""
        if limit is None:
            return _DEFAULT_LIMIT
        return min(max(0, limit), _MAX_LIMIT)

    def enforce_retention(self, max_age: timedelta | None) -> None:
        """Delete traces strictly older than *max_age*, cascading to spans (R13).

        The cutoff is ``now - max_age``; a trace is removed when its
        ``start_ts < cutoff`` — equivalently when its age (``now - start_ts``) is
        *strictly* greater than ``max_age`` (R13.1). Traces exactly at the
        boundary (age ``== max_age``) are retained. Deleting a trace removes all
        of its spans within the same cycle via the ``spans`` foreign key
        ``ON DELETE CASCADE`` (R13.2). When *max_age* is ``None`` no retention
        period is configured and every trace is retained (a no-op, R13.3).

        Failure isolation (R13.5)
        -------------------------
        The happy path issues a single set-based ``DELETE`` that runs in one
        transaction, so it is all-or-nothing: if it fails (e.g. a single trace
        cannot be removed) nothing is deleted and the cycle falls back to per-row
        deletion. The fallback re-reads the candidate ids and deletes each trace
        in its own transaction; a trace that still fails is left intact (along
        with its spans) and an error indication identifying it is recorded —
        appended to :attr:`retention_errors`, logged at WARNING, and counted via
        ``rag_trace_store_retention_failures_total`` — while the remaining traces
        are removed. A retention cycle therefore never aborts the application.
        """
        if max_age is None:
            return  # No period configured → retain everything (R13.3).

        self.retention_errors = []
        cutoff = _to_iso_utc(datetime.now(timezone.utc) - max_age)

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(DELETE_EXPIRED_TRACES_SQL, (cutoff,))
            return
        except Exception as bulk_exc:  # noqa: BLE001 - isolate per row instead of aborting
            self._enforce_retention_per_row(cutoff, bulk_exc)

    def _enforce_retention_per_row(self, cutoff: str, bulk_exc: Exception) -> None:
        """Per-row retention fallback after a failed bulk delete (R13.5).

        Re-reads the ids of traces strictly older than *cutoff* and deletes each
        one in its own transaction so a single failing trace is retained (with its
        spans) and recorded without preventing the removal of the others. If the
        candidate ids cannot even be enumerated, a single error indication is
        recorded and the cycle stops, leaving every trace intact.
        """
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(SELECT_EXPIRED_TRACE_IDS_SQL, (cutoff,))
                    trace_ids = [row[0] for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001 - cannot enumerate; record and stop
            self._record_retention_error(None, exc)
            return

        for trace_id in trace_ids:
            try:
                with self._connection_factory() as conn:
                    with conn.cursor() as cur:
                        cur.execute(DELETE_TRACE_BY_ID_SQL, (trace_id,))
            except Exception as exc:  # noqa: BLE001 - retain this trace, continue the cycle
                self._record_retention_error(trace_id, exc)

    def _record_retention_error(self, trace_id: Any | None, exc: Exception) -> None:
        """Record a failed retention deletion: keep the trace, note the error (R13.5).

        Appends an error indication to :attr:`retention_errors`, logs at WARNING
        (wrapped so a failing warning cannot itself propagate), and increments the
        retention-failure counter. Never raises.
        """
        self.retention_errors.append({"trace_id": trace_id, "error": str(exc)})
        try:
            self._logger.warning(
                "Failed to delete trace %s during retention; retaining: %s",
                trace_id,
                exc,
            )
        except Exception:  # noqa: BLE001 - a failing warning must not propagate
            pass
        self._increment(_RETENTION_FAILURE_METRIC, {})


# ---------------------------------------------------------------------------
# Timestamp / attribute helpers
# ---------------------------------------------------------------------------


def _to_iso_utc(value: datetime) -> str:
    """Render a ``datetime`` as an ISO-8601 string normalised to UTC.

    A naive ``datetime`` is assumed to already be UTC; an aware ``datetime`` is
    converted to UTC. Used to bind inclusive time-range filter bounds, mirroring
    the log store.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _coerce_utc(value: Any) -> datetime:
    """Normalise a stored timestamp to a timezone-aware UTC ``datetime``.

    Accepts either a ``datetime`` (as psycopg returns for ``timestamptz``) or an
    ISO-8601 string (as a fake connection in tests may yield), matching the log
    store's coercion.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_attributes(value: Any) -> dict[str, AttributeValue]:
    """Rebuild a span ``attributes`` map from a JSONB column value.

    psycopg returns ``jsonb`` as a Python mapping; a fake connection in tests may
    instead yield a JSON string or ``None``. All cases are normalised to a plain
    ``dict`` (empty when absent).
    """
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value)
