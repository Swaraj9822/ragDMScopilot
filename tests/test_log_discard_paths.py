"""Unit tests for :class:`PostgresLogStore` best-effort discard paths (task 10.6).

These tests target the *discard* and *no-period retention* contracts of the log
store — the behaviour that must hold when persistence cannot complete or when no
retention period is configured — driving the production store through injected
fakes so no live database is required:

* R14.5 — IF persistence of a log record fails, the platform logs the failure at
  WARNING, discards the record so no partial data remains, increments
  ``rag_log_store_write_failures_total``, and never raises to the caller,
  *including when the warning logging itself fails*.
* R18.2 — WHERE no log retention period is configured, the store retains every
  record (a no-op that never opens a connection).
* R18.4 — IF removal of a log record fails during retention, the store retains
  that record intact and records an error indication identifying it.

This module mirrors ``tests/test_trace_discard_paths.py`` and focuses narrowly on
the WARNING / discard / write-failure-metric paths and the no-period / failed
per-row retention paths. The happy-path query/search behaviour is covered by the
``test_log_*`` property suites.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rag_system.config import Settings
from rag_system.observability_tracing.log_store import PostgresLogStore
from rag_system.observability_tracing.models import LogRecordModel

_WRITE_FAILURE_METRIC = "rag_log_store_write_failures_total"
_RETENTION_FAILURE_METRIC = "rag_log_store_retention_failures_total"

_TRACE_ID = "a" * 32


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
    """A psycopg-style connection whose log INSERT always raises.

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


def _record() -> LogRecordModel:
    return LogRecordModel(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        level="ERROR",
        logger="rag_system.api",
        message="something failed",
        trace_id=_TRACE_ID,
        exc_text=None,
        extra={"route": "/ask"},
        insertion_seq=1,
    )


# ---------------------------------------------------------------------------
# R14.5 — WARNING-then-discard on persist failure + write-failure metric
# ---------------------------------------------------------------------------


def test_persist_failure_logs_warning_discards_and_counts_write_failure() -> None:
    metrics = _RecordingMetrics()
    log = _RecordingLogger()
    store = PostgresLogStore(
        _settings(),
        connection_factory=lambda: _FailingConnection(),
        metrics=metrics,
        logger_=cast(Any, log),
    )

    # persist must never raise to the caller, allowing the request to return (R14.5).
    store.persist(_record())

    # The failure is logged at WARNING with the record's trace id in the args (R14.5).
    assert len(log.warnings) == 1
    args, _kwargs = log.warnings[0]
    assert _TRACE_ID in args

    # The write-failure counter is incremented exactly once (R14.5).
    assert metrics.count(_WRITE_FAILURE_METRIC) == 1


def test_persist_failure_survives_a_failing_warning_call() -> None:
    """Even when the WARNING logging itself raises, persist discards and counts (R14.5)."""
    metrics = _RecordingMetrics()
    log = _RecordingLogger(fail=True)  # the warning call raises
    store = PostgresLogStore(
        _settings(),
        connection_factory=lambda: _FailingConnection(),
        metrics=metrics,
        logger_=cast(Any, log),
    )

    # The failing warning must not propagate; persist still returns normally (R14.5).
    store.persist(_record())

    assert len(log.warnings) == 1  # the warning was attempted
    # The write-failure metric is still recorded despite the warning failing (R14.5).
    assert metrics.count(_WRITE_FAILURE_METRIC) == 1


def test_persist_failure_tolerates_a_failing_metrics_backend() -> None:
    """A metrics backend that raises must not break the best-effort discard (R14.5)."""

    class _BrokenMetrics:
        def increment(self, name: str, labels: dict[str, Any]) -> None:
            raise RuntimeError("metrics backend is down")

    log = _RecordingLogger()
    store = PostgresLogStore(
        _settings(),
        connection_factory=lambda: _FailingConnection(),
        metrics=_BrokenMetrics(),
        logger_=cast(Any, log),
    )

    # Neither the failing write, the warning, nor the failing metric may propagate.
    store.persist(_record())
    assert len(log.warnings) == 1


# ---------------------------------------------------------------------------
# R18.2 — no retention period configured retains everything (no-op)
# ---------------------------------------------------------------------------


def test_no_retention_period_is_a_noop_that_never_opens_a_connection() -> None:
    opened = False

    def factory() -> Any:
        nonlocal opened
        opened = True
        raise AssertionError("connection should not be opened when no period is set")

    store = PostgresLogStore(_settings(), connection_factory=factory)

    store.enforce_retention(None)  # R18.2

    assert opened is False
    assert store.retention_errors == []


# ---------------------------------------------------------------------------
# R18.4 — a failed retention deletion retains the record and records an error
# ---------------------------------------------------------------------------


class _RetentionDB:
    """Minimal in-memory log-records store for the retention SQL fake."""

    def __init__(self) -> None:
        self.records: dict[int, datetime] = {}
        self.fail_bulk = False
        self.fail_ids: set[int] = set()

    def add(self, row_id: int, ts: datetime) -> None:
        self.records[row_id] = ts

    def expired(self, cutoff: datetime) -> list[int]:
        return sorted(rid for rid, ts in self.records.items() if ts < cutoff)

    def delete(self, row_id: int) -> None:
        self.records.pop(row_id, None)


class _RetentionCursor:
    def __init__(self, db: _RetentionDB) -> None:
        self._db = db
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _RetentionCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "SELECT id" in sql:  # per-row candidate enumeration
            cutoff = datetime.fromisoformat(params[0])
            self._result = [(rid,) for rid in self._db.expired(cutoff)]
        elif "WHERE ts <" in sql:  # bulk delete
            if self._db.fail_bulk:
                raise RuntimeError("bulk delete failed")
            for rid in self._db.expired(datetime.fromisoformat(params[0])):
                self._db.delete(rid)
        elif "WHERE id =" in sql:  # per-row delete
            row_id = params[0]
            if row_id in self._db.fail_ids:
                raise RuntimeError(f"cannot delete {row_id}")
            self._db.delete(row_id)
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


def test_failed_retention_deletion_retains_record_and_records_error() -> None:
    now = datetime.now(timezone.utc)
    db = _RetentionDB()
    db.add(1, now - timedelta(hours=48))  # keep_fail
    db.add(2, now - timedelta(hours=72))  # remove_ok
    db.fail_bulk = True       # force the per-row fallback
    db.fail_ids = {1}         # this row keeps failing

    metrics = _RecordingMetrics()
    log = _RecordingLogger()
    store = PostgresLogStore(
        _settings(),
        connection_factory=lambda: _RetentionConnection(db),
        metrics=metrics,
        logger_=cast(Any, log),
    )

    store.enforce_retention(timedelta(hours=24))

    # The failing record is retained intact; the other is removed (R18.4).
    assert 1 in db.records
    assert 2 not in db.records

    # An error indication identifying the failed removal is recorded (R18.4).
    assert len(store.retention_errors) == 1
    assert store.retention_errors[0]["id"] == 1
    assert "cannot delete 1" in store.retention_errors[0]["error"]

    # The failure is logged at WARNING and counted.
    assert log.warnings
    assert metrics.count(_RETENTION_FAILURE_METRIC) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
