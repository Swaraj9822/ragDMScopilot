"""Unit tests for :class:`PostgresTraceStore` best-effort discard paths (task 9.9).

These tests target the *discard* contracts of the trace store — the behaviour
that must hold when persistence or retention cannot complete — driving the
production store through injected fakes so no live database is required:

* R5.4 — IF persistence of a trace fails, the platform logs the failure at
  WARNING, discards the trace so no partial data remains, and allows the request
  to return normally, *including when the warning logging itself fails*.
* R10.3 — IF a trace-store write fails, the store discards the affected write and
  records a dropped-write metric (``rag_trace_store_write_failures_total``).
* R13.3 — WHERE no retention period is configured, the store retains every trace
  (a no-op that never opens a connection).
* R13.5 — IF removal of a trace fails during retention, the store retains that
  trace and its spans intact and records an error indication identifying it.

Property-style atomicity (R5.1/R5.5/R5.6) is covered separately by
``test_atomic_persistence_properties``; the broader retention behaviour by
``test_trace_store_retention``. This module focuses narrowly on the WARNING /
discard / dropped-write-metric paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rag_system.config import Settings
from rag_system.observability_tracing.models import Span, Trace
from rag_system.observability_tracing.trace_store import PostgresTraceStore

_WRITE_FAILURE_METRIC = "rag_trace_store_write_failures_total"
_PERSISTED_METRIC = "rag_traces_persisted_total"
_RETENTION_FAILURE_METRIC = "rag_trace_store_retention_failures_total"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingMetrics:
    """Records every counter increment as ``(name, labels)`` tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def increment(self, name: str, labels: dict[str, Any]) -> None:
        self.calls.append((name, dict(labels)))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class _RecordingLogger:
    """Captures WARNING calls; optionally raises to simulate a failing warning."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warnings.append((args, kwargs))
        if self.fail:
            raise RuntimeError("logging backend is down")


class _FailingConnection:
    """A psycopg-style connection whose trace INSERT always raises.

    Exiting via the exception rolls back (no data is retained) and re-raises so
    the store's own best-effort handler runs.
    """

    def __enter__(self) -> _FailingConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False  # propagate any exception → rollback semantics

    def cursor(self) -> _FailingConnection:
        return self

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        raise RuntimeError("connection refused")


def _settings() -> Settings:
    return cast(Settings, SimpleNamespace())


def _trace() -> Trace:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Trace(
        trace_id="a" * 32,
        route="/ask",
        start_ts=now,
        duration_ms=5,
        root_status="success",
        spans=[
            Span(
                span_id="root",
                parent_span_id=None,
                operation="POST /ask",
                start_ts=now,
                duration_ms=5,
                status="success",
                attributes={"http.status_code": 200},
            )
        ],
    )


# ---------------------------------------------------------------------------
# R5.4 / R10.3 — WARNING-then-discard on persist failure + dropped-write metric
# ---------------------------------------------------------------------------


def test_persist_failure_logs_warning_discards_and_counts_dropped_write() -> None:
    metrics = _RecordingMetrics()
    log = _RecordingLogger()
    store = PostgresTraceStore(
        _settings(),
        connection_factory=lambda: _FailingConnection(),
        metrics=metrics,
        logger_=cast(Any, log),
    )

    # persist must never raise to the caller, allowing the request to return (R5.4).
    store.persist(_trace())

    # The failure is logged at WARNING with the trace id in the message (R5.4).
    assert len(log.warnings) == 1
    args, _kwargs = log.warnings[0]
    assert "a" * 32 in args

    # The dropped-write counter is incremented for the affected route (R10.3).
    assert metrics.count(_WRITE_FAILURE_METRIC) == 1
    assert (_WRITE_FAILURE_METRIC, {"route": "/ask"}) in metrics.calls

    # The trace is discarded, so the persisted-traces counter never advances (R5.6).
    assert metrics.count(_PERSISTED_METRIC) == 0


def test_persist_failure_survives_a_failing_warning_call() -> None:
    """Even when the WARNING logging itself raises, persist discards and counts."""
    metrics = _RecordingMetrics()
    log = _RecordingLogger(fail=True)  # the warning call raises
    store = PostgresTraceStore(
        _settings(),
        connection_factory=lambda: _FailingConnection(),
        metrics=metrics,
        logger_=cast(Any, log),
    )

    # The failing warning must not propagate; persist still returns normally (R5.4).
    store.persist(_trace())

    assert len(log.warnings) == 1  # the warning was attempted
    # The dropped-write metric is still recorded despite the warning failing (R10.3).
    assert metrics.count(_WRITE_FAILURE_METRIC) == 1
    assert metrics.count(_PERSISTED_METRIC) == 0


def test_persist_failure_tolerates_a_failing_metrics_backend() -> None:
    """A metrics backend that raises must not break the best-effort discard (R10.3)."""

    class _BrokenMetrics:
        def increment(self, name: str, labels: dict[str, Any]) -> None:
            raise RuntimeError("metrics backend is down")

    log = _RecordingLogger()
    store = PostgresTraceStore(
        _settings(),
        connection_factory=lambda: _FailingConnection(),
        metrics=_BrokenMetrics(),
        logger_=cast(Any, log),
    )

    # Neither the failing write, the warning, nor the failing metric may propagate.
    store.persist(_trace())
    assert len(log.warnings) == 1


# ---------------------------------------------------------------------------
# R13.3 — no retention period configured retains everything (no-op)
# ---------------------------------------------------------------------------


def test_no_retention_period_is_a_noop_that_never_opens_a_connection() -> None:
    opened = False

    def factory() -> Any:
        nonlocal opened
        opened = True
        raise AssertionError("connection should not be opened when no period is set")

    store = PostgresTraceStore(_settings(), connection_factory=factory)

    store.enforce_retention(None)  # R13.3

    assert opened is False
    assert store.retention_errors == []


# ---------------------------------------------------------------------------
# R13.5 — a failed retention deletion retains the trace and records an error
# ---------------------------------------------------------------------------


class _RetentionDB:
    """Minimal in-memory traces store for the retention SQL fake."""

    def __init__(self) -> None:
        self.traces: dict[str, datetime] = {}
        self.spans: dict[str, list[str]] = {}
        self.fail_bulk = False
        self.fail_ids: set[str] = set()

    def add(self, trace_id: str, start_ts: datetime, span_ids: list[str]) -> None:
        self.traces[trace_id] = start_ts
        self.spans[trace_id] = list(span_ids)

    def expired(self, cutoff: datetime) -> list[str]:
        return sorted(tid for tid, ts in self.traces.items() if ts < cutoff)

    def delete(self, trace_id: str) -> None:
        self.traces.pop(trace_id, None)
        self.spans.pop(trace_id, None)


class _RetentionCursor:
    def __init__(self, db: _RetentionDB) -> None:
        self._db = db
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _RetentionCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "SELECT trace_id" in sql:
            cutoff = datetime.fromisoformat(params[0])
            self._result = [(tid,) for tid in self._db.expired(cutoff)]
        elif "WHERE start_ts" in sql:  # bulk delete
            if self._db.fail_bulk:
                raise RuntimeError("bulk delete failed")
            for tid in self._db.expired(datetime.fromisoformat(params[0])):
                self._db.delete(tid)
        elif "WHERE trace_id" in sql:  # per-row delete
            trace_id = params[0]
            if trace_id in self._db.fail_ids:
                raise RuntimeError(f"cannot delete {trace_id}")
            self._db.delete(trace_id)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _RetentionConnection:
    def __init__(self, db: _RetentionDB) -> None:
        self._db = db

    def __enter__(self) -> _RetentionConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _RetentionCursor:
        return _RetentionCursor(self._db)


def test_failed_retention_deletion_retains_trace_and_records_error() -> None:
    now = datetime.now(timezone.utc)
    db = _RetentionDB()
    db.add("keep_fail", now - timedelta(hours=48), ["s1"])
    db.add("remove_ok", now - timedelta(hours=72), ["s2"])
    db.fail_bulk = True          # force the per-row fallback
    db.fail_ids = {"keep_fail"}  # this row keeps failing

    metrics = _RecordingMetrics()
    log = _RecordingLogger()
    store = PostgresTraceStore(
        _settings(),
        connection_factory=lambda: _RetentionConnection(db),
        metrics=metrics,
        logger_=cast(Any, log),
    )

    store.enforce_retention(timedelta(hours=24))

    # The failing trace and its spans are retained intact; the other is removed (R13.5).
    assert "keep_fail" in db.traces
    assert db.spans["keep_fail"] == ["s1"]
    assert "remove_ok" not in db.traces

    # An error indication identifying the failed removal is recorded (R13.5).
    assert len(store.retention_errors) == 1
    assert store.retention_errors[0]["trace_id"] == "keep_fail"
    assert "keep_fail" in store.retention_errors[0]["error"]

    # The failure is logged at WARNING and counted.
    assert log.warnings
    assert metrics.count(_RETENTION_FAILURE_METRIC) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
