"""PostgreSQL-backed log store for the AI observability platform.

:class:`PostgresLogStore` is the durable persistence layer for structured log
records. It mirrors :class:`rag_system.observability_tracing.trace_store.PostgresTraceStore`
in construction and failure handling: it reuses the schema and ``COPILOT_DB_*``
connection settings from :mod:`rag_system.observability_tracing.schema`, the
psycopg connection style of :class:`rag_system.copilot.PostgresCopilotExecutor`,
and the :class:`~rag_system.observability_tracing.log_serializer.LogSerializer`
to convert an in-memory :class:`LogRecordModel` into its stored representation.

This module implements task 10.1: :meth:`PostgresLogStore.persist`,
:meth:`PostgresLogStore.get_by_trace`, and :meth:`PostgresLogStore.search`. The
retention method (:meth:`PostgresLogStore.enforce_retention`) is scaffolded here
and implemented by task 10.2.

Requirements covered:

* R14.1 / R14.2 / R14.4 — every field of an emitted log record (UTC timestamp,
  level, logger name, message, trace_id correlation, exc_text, extra) is
  persisted to the ``log_records`` table; the database assigns the row ``id`` in
  insertion order.
* R14.3 — a null or absent ``trace_id`` is persisted as an explicit SQL ``NULL``.
* R14.5 — persistence is best-effort: on failure the store logs at WARNING
  (wrapped so a failing warning call cannot itself raise), discards the record,
  increments ``rag_log_store_write_failures_total``, and never raises to the
  caller.
* R15.1 / R15.2 — :meth:`get_by_trace` returns every record whose ``trace_id``
  equals the supplied value, ordered by timestamp descending with ties broken by
  insertion order (``id``) descending.
* R16.1-R16.6 / R16.9 — :meth:`search` applies an inclusive ``[start, end]`` time
  range, case-sensitive ``level`` and ``trace_id`` equality, AND semantics across
  supplied filters, a default limit of 100 capped at 1000, and timestamp
  descending order (ties by ``id`` descending).

Testability
-----------
Like the trace store, the log store accepts an injected ``connection_factory``,
``serializer``, and ``metrics`` so its behaviour can be exercised without a live
database. The factory is any zero-argument callable returning a psycopg-style
connection usable as a context manager (commit + close on a clean exit, rollback
on an exception). When no factory is supplied, the store connects using the
``COPILOT_DB_*`` settings via :func:`rag_system.observability_tracing.schema.connect`.
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
from .log_serializer import LogSerializer
from .models import AttributeValue, LogRecordModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    import logging
    from contextlib import AbstractContextManager

logger = get_logger(__name__)

__all__ = [
    "INSERT_LOG_SQL",
    "LogSearchFilters",
    "PostgresLogStore",
]

#: Metric incremented once per failed log-store write (R14.5).
_WRITE_FAILURE_METRIC = "rag_log_store_write_failures_total"

#: Metric incremented once per log record whose retention deletion failed (R18.4).
_RETENTION_FAILURE_METRIC = "rag_log_store_retention_failures_total"

#: Default and maximum result limits for :meth:`PostgresLogStore.search` (R16.5, R16.6).
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


# ---------------------------------------------------------------------------
# Insert statement (the DB assigns ``id`` from insertion order — R15.2)
# ---------------------------------------------------------------------------

INSERT_LOG_SQL = """
    INSERT INTO log_records (ts, level, logger, message, trace_id, exc_text, extra)
    VALUES (%s::timestamptz, %s, %s, %s, %s, %s, %s::jsonb)
"""
"""Insert a single ``log_records`` row; ``extra`` is passed as a JSON string and
cast to ``jsonb`` so the store does not depend on a psycopg JSON adapter. ``ts``
is an ISO-8601 UTC string cast to ``timestamptz`` (R14.2); a null ``trace_id`` is
written as an explicit SQL ``NULL`` (R14.3)."""

#: Columns selected by the read queries, in the order :meth:`_row_to_record` maps.
_SELECT_COLUMNS = "ts, level, logger, message, trace_id, exc_text, extra, id"

SELECT_BY_TRACE_SQL = f"""
    SELECT {_SELECT_COLUMNS}
    FROM log_records
    WHERE trace_id = %s
    ORDER BY ts DESC, id DESC
"""
"""All records for a ``trace_id``, ordered by timestamp then insertion order,
both descending (R15.1, R15.2)."""

# ---------------------------------------------------------------------------
# Retention statements (task 10.2 — R18)
# ---------------------------------------------------------------------------

DELETE_EXPIRED_LOGS_SQL = """
    DELETE FROM log_records
    WHERE ts < %s::timestamptz
"""
"""Bulk-delete every record strictly older than the cutoff (``ts < cutoff`` ⇔
``now - ts > max_age``); rows exactly at the boundary (``ts == cutoff``) are
retained (R18.1)."""

SELECT_EXPIRED_LOG_IDS_SQL = """
    SELECT id FROM log_records
    WHERE ts < %s::timestamptz
    ORDER BY id
"""
"""Candidate row ids for per-row retention when the bulk delete fails, so each
removal can be isolated (R18.4)."""

DELETE_LOG_BY_ID_SQL = """
    DELETE FROM log_records
    WHERE id = %s
"""
"""Delete a single record by its insertion-order ``id`` during the per-row
retention fallback so a failing row does not abort the whole cycle (R18.4)."""


# ---------------------------------------------------------------------------
# Search filters (mirrors the trace store / test double filter shape)
# ---------------------------------------------------------------------------


@dataclass
class LogSearchFilters:
    """Filters for :meth:`PostgresLogStore.search` (R16).

    Mirrors the in-memory store double's ``LogSearchFilters`` so the query
    implementation and its callers share a single filter shape.
    """

    start: datetime | None = None            # inclusive lower bound (R16.1)
    end: datetime | None = None              # inclusive upper bound (R16.1)
    level: str | None = None                 # case-sensitive equality (R16.2)
    trace_id: str | None = None              # case-sensitive equality (R16.3)
    limit: int = 100                         # default 100, capped 1000 (R16.5, R16.6)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PostgresLogStore:
    """Durable, queryable log persistence backed by PostgreSQL.

    Args:
        settings: Application settings carrying the ``COPILOT_DB_*`` connection
            values, reused by the default connection factory.
        connection_factory: Optional zero-argument callable returning a
            psycopg-style connection usable as a context manager (commit + close
            on clean exit, rollback on exception). Defaults to connecting via
            :func:`schema.connect`. Injecting a factory makes the persist/query
            behaviour testable without a live database.
        serializer: Optional :class:`LogSerializer` (LogRecordModel -> StoredLog).
        metrics: Optional metrics registry; defaults to the shared
            :data:`rag_system.observability.metrics` registry.
        logger_: Optional logger; defaults to this module's logger.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        serializer: LogSerializer | None = None,
        metrics: Any | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._connection_factory = connection_factory or self._default_connection_factory
        self._serializer = serializer or LogSerializer()
        self._metrics = metrics if metrics is not None else _default_metrics
        self._logger = logger_ or logger
        #: Error indications for records whose retention deletion failed (R18.4).
        #: Each entry is ``{"id": <row id or None>, "error": <str>}``; the most
        #: recent :meth:`enforce_retention` cycle's failures are appended here.
        self.retention_errors: list[dict[str, Any]] = []

    # -- connection ---------------------------------------------------------

    def _default_connection_factory(self) -> AbstractContextManager[Any]:
        """Open a psycopg connection using the ``COPILOT_DB_*`` settings.

        psycopg's connection context manager commits (and closes) on a clean
        exit and rolls back on an exception, matching the trace store.
        """
        return schema.connect(self._settings)

    # -- persist ------------------------------------------------------------

    def persist(self, record: LogRecordModel) -> None:
        """Persist *record* to the ``log_records`` table (best-effort).

        Every field of the record is written; an absent ``trace_id`` is stored as
        an explicit SQL ``NULL`` (R14.3) and the database assigns the row ``id``
        in insertion order (R15.2). On any failure the record is discarded: the
        failure is logged at WARNING (wrapped so a failing warning call cannot
        itself raise), the write-failure counter is incremented, and nothing is
        raised to the caller (R14.5).
        """
        try:
            stored = self._serializer.serialize(record)
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        INSERT_LOG_SQL,
                        (
                            stored["timestamp"],
                            stored["level"],
                            stored["logger"],
                            stored["message"],
                            # Explicit null when absent (R14.3).
                            stored["trace_id"],
                            stored["exc_text"],
                            json.dumps(stored["extra"]),
                        ),
                    )
        except Exception as exc:  # noqa: BLE001 - best-effort: never raise to caller
            self._handle_failure(record, exc)

    def _handle_failure(self, record: LogRecordModel, exc: Exception) -> None:
        """Best-effort failure handling: discard the record and never raise (R14.5).

        Logs at WARNING wrapped in a bare ``try/except`` so that a failure inside
        the warning call itself cannot propagate, then increments the
        write-failure counter.
        """
        trace_id = getattr(record, "trace_id", None)
        try:
            self._logger.warning(
                "Failed to persist log record (trace %s); discarding: %s",
                trace_id,
                exc,
                extra={"trace_id": trace_id} if isinstance(trace_id, str) else None,
            )
        except Exception:  # noqa: BLE001 - a failing warning must not propagate (R14.5)
            pass
        self._increment(_WRITE_FAILURE_METRIC, {})

    def _increment(self, name: str, labels: dict[str, Any]) -> None:
        """Increment a counter, tolerating a metrics backend that raises."""
        try:
            self._metrics.increment(name, labels)
        except Exception:  # noqa: BLE001 - metrics must never break persistence
            pass

    # -- query --------------------------------------------------------------

    def get_by_trace(self, trace_id: str) -> list[LogRecordModel]:
        """Return all records whose ``trace_id`` equals *trace_id*.

        Records are ordered by timestamp descending, ties broken by insertion
        order (``id``) descending (R15.1, R15.2). An empty list is returned when
        no records match.
        """
        return self._fetch(SELECT_BY_TRACE_SQL, (trace_id,))

    def search(self, filters: LogSearchFilters) -> list[LogRecordModel]:
        """Return records matching every supplied filter (AND semantics).

        Applies an inclusive ``[start, end]`` time range (R16.1), case-sensitive
        ``level`` (R16.2) and ``trace_id`` (R16.3) equality, ordered by timestamp
        descending with ties broken by insertion order descending, and capped at
        the effective limit (default 100, max 1000 — R16.5, R16.6). An empty list
        is returned when nothing matches (R16.9).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if filters.start is not None:
            clauses.append("ts >= %s::timestamptz")
            params.append(_to_iso_utc(filters.start))
        if filters.end is not None:
            clauses.append("ts <= %s::timestamptz")
            params.append(_to_iso_utc(filters.end))
        if filters.level is not None:
            clauses.append("level = %s")
            params.append(filters.level)
        if filters.trace_id is not None:
            clauses.append("trace_id = %s")
            params.append(filters.trace_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = self._effective_limit(filters.limit)
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM log_records\n"
            f"    {where}\n"
            "    ORDER BY ts DESC, id DESC\n"
            "    LIMIT %s"
        )
        params.append(limit)
        return self._fetch(sql, tuple(params))

    def _fetch(self, sql: str, params: tuple[Any, ...]) -> list[LogRecordModel]:
        """Execute a read query and map each row to a :class:`LogRecordModel`."""
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: Any) -> LogRecordModel:
        """Map a ``log_records`` row (``_SELECT_COLUMNS`` order) to a model.

        The database returns ``ts`` as a timezone-aware ``datetime`` and ``extra``
        as a mapping (JSONB); the row ``id`` becomes the insertion-order tiebreaker
        (R15.2).
        """
        ts, level, logger_name, message, trace_id, exc_text, extra, row_id = row
        raw_extra: dict[str, AttributeValue] = dict(extra) if extra else {}
        return LogRecordModel(
            timestamp=_coerce_utc(ts),
            level=level,
            logger=logger_name,
            message=message,
            trace_id=trace_id,
            exc_text=exc_text,
            extra=raw_extra,
            insertion_seq=int(row_id),
        )

    @staticmethod
    def _effective_limit(limit: int | None) -> int:
        """Clamp a requested limit to ``[0, 1000]``; ``None`` defaults to 100."""
        if limit is None:
            return _DEFAULT_LIMIT
        return min(max(0, limit), _MAX_LIMIT)

    # -- retention (task 10.2) ----------------------------------------------

    def enforce_retention(self, max_age: timedelta | None) -> None:
        """Delete log records strictly older than *max_age* (task 10.2, R18).

        The cutoff is ``now - max_age``; a record is removed when ``ts < cutoff``
        — equivalently when its age (``now - ts``) is *strictly* greater than
        ``max_age`` (R18.1). Records exactly at the boundary (age ``== max_age``)
        are retained. When *max_age* is ``None`` no retention period is configured
        and every record is retained (a no-op, R18.2).

        Failure isolation (R18.4)
        -------------------------
        The happy path issues a single set-based ``DELETE``. Because that runs in
        one transaction it is all-or-nothing, so if it fails (e.g. a single row
        cannot be removed) nothing is deleted and the cycle falls back to
        per-row deletion: it re-reads the candidate ids and deletes each in its
        own transaction. A row that still fails is left intact and an error
        indication identifying it is recorded — appended to
        :attr:`retention_errors`, logged at WARNING, and counted via
        ``rag_log_store_retention_failures_total`` — while the remaining rows are
        deleted. A retention cycle therefore never aborts the application.
        """
        if max_age is None:
            return  # No period configured → retain everything (R18.2).

        self.retention_errors = []
        cutoff = _to_iso_utc(datetime.now(timezone.utc) - max_age)

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(DELETE_EXPIRED_LOGS_SQL, (cutoff,))
            return
        except Exception as bulk_exc:  # noqa: BLE001 - isolate per row instead of aborting
            self._enforce_retention_per_row(cutoff, bulk_exc)

    def _enforce_retention_per_row(self, cutoff: str, bulk_exc: Exception) -> None:
        """Per-row retention fallback after a failed bulk delete (R18.4).

        Re-reads the ids of records strictly older than *cutoff* and deletes each
        one in its own transaction so a single failing row is retained and
        recorded without preventing the removal of the others.
        """
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(SELECT_EXPIRED_LOG_IDS_SQL, (cutoff,))
                    ids = [row[0] for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001 - cannot enumerate; record and stop
            self._record_retention_error(None, exc)
            return

        for row_id in ids:
            try:
                with self._connection_factory() as conn:
                    with conn.cursor() as cur:
                        cur.execute(DELETE_LOG_BY_ID_SQL, (row_id,))
            except Exception as exc:  # noqa: BLE001 - retain this row, continue the cycle
                self._record_retention_error(row_id, exc)

    def _record_retention_error(self, row_id: Any | None, exc: Exception) -> None:
        """Record a failed retention deletion: keep the row, note the error (R18.4).

        Appends an error indication to :attr:`retention_errors`, logs at WARNING
        (wrapped so a failing warning cannot itself propagate), and increments the
        retention-failure counter. Never raises.
        """
        self.retention_errors.append({"id": row_id, "error": str(exc)})
        try:
            self._logger.warning(
                "Failed to delete log record %s during retention; retaining: %s",
                row_id,
                exc,
            )
        except Exception:  # noqa: BLE001 - a failing warning must not propagate
            pass
        self._increment(_RETENTION_FAILURE_METRIC, {})


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _to_iso_utc(value: datetime) -> str:
    """Render a ``datetime`` as an ISO-8601 string normalised to UTC.

    A naive ``datetime`` is assumed to already be UTC; an aware ``datetime`` is
    converted to UTC. Reused for binding inclusive time-range filter bounds.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _coerce_utc(value: Any) -> datetime:
    """Normalise a stored timestamp to a timezone-aware UTC ``datetime``.

    Accepts either a ``datetime`` (as psycopg returns for ``timestamptz``) or an
    ISO-8601 string (as a fake connection in tests may yield).
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
