"""In-memory transactional store double for the AI observability platform.

This is **test support**, not a test module. The filename intentionally omits
the ``test_`` prefix so pytest does not collect it; property tests import it by
module name (the repo already imports test/support modules this way, e.g.
``import test_rag_flow_integration`` in ``tests/test_preservation_properties.py``).

Purpose
-------
Task 8.2 of the ``ai-observability-platform`` spec calls for an in-memory
transactional store double so the atomicity properties (R5.1, R5.5) and the
retention properties (R13, R18) can be exercised without a live PostgreSQL
database. The double models the exact operations the real
``PostgresTraceStore`` / ``PostgresLogStore`` will perform:

* ``begin`` / stage inserts / ``commit`` / ``rollback`` -- a staging area with
  atomic commit and full rollback, mirroring a single psycopg transaction
  (``with conn: ...`` then ``conn.rollback()`` as seen in
  ``rag_system.copilot.PostgresCopilotExecutor``).
* ``persist`` -- writes a trace row plus all of its span rows inside one
  transaction; a forced failure while staging any span rolls the whole
  transaction back, leaving **no partial data** (R5.5).
* ``get_trace`` / ``search_traces`` / ``enforce_retention`` -- the trace query
  and retention semantics the property tests exercise (R7, R8, R13).
* ``persist_log`` / ``get_logs_by_trace`` / ``search_logs`` /
  ``enforce_log_retention`` -- the log equivalents (R14, R15, R16, R18).

The double stores deep copies on commit and returns deep copies on read, so it
behaves like a real database boundary: callers cannot mutate committed state by
holding on to a reference.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from rag_system.observability_tracing.models import (
    LogRecordModel,
    Span,
    Trace,
)


def _utcnow() -> datetime:
    """Current UTC time, matching the UTC timestamps stored in domain models."""
    return datetime.now(tz=timezone.utc)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StoreError(RuntimeError):
    """Raised by the double to simulate a database/transaction failure.

    Property tests inject this (directly or via a ``fail_on`` predicate) to
    force a rollback mid-staging and assert that nothing partial survives.
    """


# ---------------------------------------------------------------------------
# Query filter shapes (subset the real store will accept)
# ---------------------------------------------------------------------------


@dataclass
class TraceSearchFilters:
    """Filters for :meth:`InMemoryTransactionalStore.search_traces` (R8)."""

    start: datetime | None = None            # inclusive lower bound (R8.1)
    end: datetime | None = None              # inclusive upper bound (R8.1)
    route: str | None = None                 # case-sensitive (R8.2)
    status: str | None = None                # case-sensitive (R8.3)
    min_duration_ms: int | None = None       # >= (R8.4)
    limit: int = 100                         # default 100, capped 1000 (R8.6, R8.7)


@dataclass
class LogSearchFilters:
    """Filters for :meth:`InMemoryTransactionalStore.search_logs` (R16)."""

    start: datetime | None = None            # inclusive lower bound (R16.1)
    end: datetime | None = None              # inclusive upper bound (R16.1)
    level: str | None = None                 # case-sensitive (R16.2)
    trace_id: str | None = None              # case-sensitive (R16.3)
    limit: int = 100                         # default 100, capped 1000 (R16.5, R16.6)


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


# ---------------------------------------------------------------------------
# Transaction (staging area)
# ---------------------------------------------------------------------------


@dataclass
class _Transaction:
    """A unit of work that stages writes before an atomic commit.

    Staged writes live entirely inside this object. They are applied to the
    owning store only by :meth:`InMemoryTransactionalStore.commit`; a
    :meth:`InMemoryTransactionalStore.rollback` (or any exception escaping the
    :meth:`InMemoryTransactionalStore.transaction` context) discards them, so
    the committed state never sees partial data.
    """

    staged_traces: dict[str, Trace] = field(default_factory=dict)
    staged_logs: list[LogRecordModel] = field(default_factory=list)
    active: bool = True

    def stage_trace(self, trace: Trace) -> None:
        if not self.active:
            raise StoreError("cannot stage onto a closed transaction")
        # Deep copy so later caller mutations don't leak into staged state.
        self.staged_traces[trace.trace_id] = copy.deepcopy(trace)

    def stage_log(self, record: LogRecordModel) -> None:
        if not self.active:
            raise StoreError("cannot stage onto a closed transaction")
        self.staged_logs.append(copy.deepcopy(record))


# ---------------------------------------------------------------------------
# The store double
# ---------------------------------------------------------------------------


class InMemoryTransactionalStore:
    """In-memory double mirroring ``PostgresTraceStore`` / ``PostgresLogStore``.

    Trace persistence is atomic: a trace and all of its spans either all commit
    or none do (R5.1, R5.5). Retention removes only entries strictly older than
    the configured period and retains boundary entries (R13.1, R18.1); a
    per-row failure leaves that row intact and records an error indication
    (R13.5, R18.4).
    """

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._logs: list[LogRecordModel] = []
        self._log_seq: int = 0
        #: Error indications recorded when a retention deletion fails (R13.5/R18.4).
        self.retention_errors: list[str] = []

    # -- low-level transaction API -----------------------------------------

    def begin(self) -> _Transaction:
        """Open a new staging transaction."""
        return _Transaction()

    def commit(self, txn: _Transaction) -> None:
        """Atomically apply every staged write, then close the transaction."""
        if not txn.active:
            raise StoreError("transaction already closed")
        # Apply in one synchronous step; nothing here can partially fail because
        # all validation/failure injection happens during staging.
        for trace_id, trace in txn.staged_traces.items():
            self._traces[trace_id] = trace
        for record in txn.staged_logs:
            self._log_seq += 1
            record.insertion_seq = self._log_seq
            self._logs.append(record)
        txn.active = False

    def rollback(self, txn: _Transaction) -> None:
        """Discard all staged writes, leaving committed state untouched."""
        txn.staged_traces.clear()
        txn.staged_logs.clear()
        txn.active = False

    @contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        """Context manager that commits on success and rolls back on error.

        Mirrors psycopg's ``with conn: ...`` transaction block: the body stages
        writes; a normal exit commits; any exception triggers a full rollback
        and is re-raised.
        """
        txn = self.begin()
        try:
            yield txn
        except BaseException:
            self.rollback(txn)
            raise
        else:
            self.commit(txn)

    # -- trace store mirror -------------------------------------------------

    def persist(
        self,
        trace: Trace,
        *,
        fail_on_span: Callable[[Span], bool] | None = None,
        fail: bool = False,
    ) -> None:
        """Persist a trace and all of its spans in a single atomic transaction.

        ``fail`` forces a failure after staging the trace row but before commit.
        ``fail_on_span`` forces a failure while staging the first span for which
        the predicate returns ``True``. Either way the transaction is rolled
        back fully and a :class:`StoreError` is raised, leaving no partial data
        (R5.5). The caller (the real flush worker) is responsible for the
        WARNING-log / discard / counter behaviour on top of this.
        """
        with self.transaction() as txn:
            txn.stage_trace(trace)
            for span in trace.spans:
                if fail_on_span is not None and fail_on_span(span):
                    raise StoreError(
                        f"forced span staging failure for span_id={span.span_id!r}"
                    )
            if fail:
                raise StoreError(f"forced persist failure for trace_id={trace.trace_id!r}")

    def get_trace(self, trace_id: str) -> Trace | None:
        """Return the persisted trace, with spans ordered ascending by start
        timestamp then ``span_id`` (root's ``parent_span_id`` is null). Returns
        ``None`` when absent (R7.1, R7.2, R7.5).
        """
        trace = self._traces.get(trace_id)
        if trace is None:
            return None
        result = copy.deepcopy(trace)
        result.spans.sort(key=lambda s: (s.start_ts, s.span_id))
        return result

    def search_traces(self, filters: TraceSearchFilters) -> list[Trace]:
        """Return traces matching every filter (AND), ordered descending by
        start timestamp, capped at the effective limit (R8.1-R8.7).
        """
        matches = [
            trace for trace in self._traces.values() if self._trace_matches(trace, filters)
        ]
        matches.sort(key=lambda t: t.start_ts, reverse=True)
        limit = self._effective_limit(filters.limit)
        return [copy.deepcopy(trace) for trace in matches[:limit]]

    @staticmethod
    def _trace_matches(trace: Trace, filters: TraceSearchFilters) -> bool:
        if filters.start is not None and trace.start_ts < filters.start:
            return False
        if filters.end is not None and trace.start_ts > filters.end:
            return False
        if filters.route is not None and trace.route != filters.route:
            return False
        if filters.status is not None and trace.root_status != filters.status:
            return False
        if filters.min_duration_ms is not None and trace.duration_ms < filters.min_duration_ms:
            return False
        return True

    def enforce_retention(
        self,
        max_age: timedelta | None,
        *,
        now: datetime | None = None,
        fail_on: Callable[[Trace], bool] | None = None,
    ) -> None:
        """Delete traces strictly older than ``max_age`` (cascading to their
        spans, which are embedded in the trace). Boundary traces (age exactly
        equal to ``max_age``) are retained. When ``max_age`` is ``None`` nothing
        is removed (R13.1, R13.2, R13.3). ``fail_on`` simulates a per-row
        deletion failure: that trace is retained intact and an error indication
        is recorded (R13.5).
        """
        if max_age is None:
            return
        reference = now if now is not None else _utcnow()
        for trace_id in list(self._traces):
            trace = self._traces[trace_id]
            if reference - trace.start_ts <= max_age:
                continue  # within retention period -> keep (boundary inclusive)
            if fail_on is not None and fail_on(trace):
                self.retention_errors.append(
                    f"failed to remove trace_id={trace_id!r}"
                )
                continue
            del self._traces[trace_id]  # cascade: embedded spans go with it

    # -- log store mirror ---------------------------------------------------

    def persist_log(self, record: LogRecordModel, *, fail: bool = False) -> None:
        """Persist a single log record atomically. ``trace_id`` of ``None`` is
        stored as an explicit null (R14.3). ``fail`` forces a rollback before
        commit, leaving no partial data (R14.5 best-effort handling sits on top).
        """
        with self.transaction() as txn:
            txn.stage_log(record)
            if fail:
                raise StoreError("forced log persist failure")

    def get_logs_by_trace(self, trace_id: str) -> list[LogRecordModel]:
        """Return all records for ``trace_id`` ordered by timestamp descending,
        ties broken by insertion order descending (R15.1, R15.2).
        """
        matches = [record for record in self._logs if record.trace_id == trace_id]
        matches.sort(key=lambda r: (r.timestamp, r.insertion_seq), reverse=True)
        return [copy.deepcopy(record) for record in matches]

    def search_logs(self, filters: LogSearchFilters) -> list[LogRecordModel]:
        """Return records matching every filter (AND), ordered descending by
        timestamp (ties by insertion order descending), capped at the effective
        limit (R16.1-R16.6).
        """
        matches = [
            record for record in self._logs if self._log_matches(record, filters)
        ]
        matches.sort(key=lambda r: (r.timestamp, r.insertion_seq), reverse=True)
        limit = self._effective_limit(filters.limit)
        return [copy.deepcopy(record) for record in matches[:limit]]

    @staticmethod
    def _log_matches(record: LogRecordModel, filters: LogSearchFilters) -> bool:
        if filters.start is not None and record.timestamp < filters.start:
            return False
        if filters.end is not None and record.timestamp > filters.end:
            return False
        if filters.level is not None and record.level != filters.level:
            return False
        if filters.trace_id is not None and record.trace_id != filters.trace_id:
            return False
        return True

    def enforce_log_retention(
        self,
        max_age: timedelta | None,
        *,
        now: datetime | None = None,
        fail_on: Callable[[LogRecordModel], bool] | None = None,
    ) -> None:
        """Delete log records strictly older than ``max_age``; retain boundary
        records; retain everything when ``max_age`` is ``None`` (R18.1, R18.2).
        ``fail_on`` simulates a per-row deletion failure: that record is retained
        and an error indication is recorded (R18.4).
        """
        if max_age is None:
            return
        reference = now if now is not None else _utcnow()
        kept: list[LogRecordModel] = []
        for record in self._logs:
            if reference - record.timestamp <= max_age:
                kept.append(record)
                continue
            if fail_on is not None and fail_on(record):
                self.retention_errors.append(
                    f"failed to remove log insertion_seq={record.insertion_seq}"
                )
                kept.append(record)
                continue
            # otherwise dropped
        self._logs = kept

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _effective_limit(limit: int | None) -> int:
        if limit is None:
            return _DEFAULT_LIMIT
        return min(max(0, limit), _MAX_LIMIT)
