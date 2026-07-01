"""Unit tests for :meth:`PostgresTraceStore.enforce_retention` (task 9.3, R13).

These tests drive the store through an injected fake psycopg-style connection so
the retention SQL and its failure-isolation fallback are exercised without a live
database. The fake interprets the three retention statements
(``DELETE_EXPIRED_TRACES_SQL``, ``SELECT_EXPIRED_TRACE_IDS_SQL``,
``DELETE_TRACE_BY_ID_SQL``) against an in-memory set of traces+spans so we can
assert:

* R13.1 — traces strictly older than the period are removed; a trace exactly at
  the boundary (age == max_age) is retained.
* R13.2 — removing a trace cascades to its spans within the same cycle.
* R13.3 — ``max_age is None`` retains everything (and never opens a connection).
* R13.5 — when a deletion fails, the bulk delete is all-or-nothing and the cycle
  falls back to per-row deletion: the failing trace (and its spans) is retained
  intact, an error indication identifying it is recorded, and the other traces
  are still removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rag_system.config import Settings
from rag_system.observability_tracing import trace_store as trace_store_module
from rag_system.observability_tracing.trace_store import PostgresTraceStore


# ---------------------------------------------------------------------------
# Fake psycopg-style connection backed by an in-memory trace/span store
# ---------------------------------------------------------------------------


class _FakeDB:
    """In-memory traces+spans the fake connection mutates, plus failure switches."""

    def __init__(self) -> None:
        # trace_id -> start_ts (UTC datetime)
        self.traces: dict[str, datetime] = {}
        # trace_id -> list of span ids (cascade target)
        self.spans: dict[str, list[str]] = {}
        #: When True, the set-based bulk DELETE raises (forces per-row fallback).
        self.fail_bulk = False
        #: trace_ids whose per-row DELETE raises (retained + recorded).
        self.fail_ids: set[str] = set()
        #: When True, enumerating candidate ids raises.
        self.fail_select = False

    def add(self, trace_id: str, start_ts: datetime, span_ids: list[str]) -> None:
        self.traces[trace_id] = start_ts
        self.spans[trace_id] = list(span_ids)

    def _delete(self, trace_id: str) -> None:
        self.traces.pop(trace_id, None)
        self.spans.pop(trace_id, None)  # cascade to spans (R13.2)

    def expired(self, cutoff: datetime) -> list[str]:
        return sorted(tid for tid, ts in self.traces.items() if ts < cutoff)


class _FakeCursor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "SELECT trace_id" in sql:
            if self._db.fail_select:
                raise RuntimeError("cannot enumerate expired traces")
            cutoff = _parse(params[0])
            self._result = [(tid,) for tid in self._db.expired(cutoff)]
        elif "WHERE start_ts" in sql:  # bulk delete
            if self._db.fail_bulk:
                raise RuntimeError("bulk delete failed")
            cutoff = _parse(params[0])
            for tid in self._db.expired(cutoff):
                self._db._delete(tid)
        elif "WHERE trace_id" in sql:  # per-row delete
            trace_id = params[0]
            if trace_id in self._db.fail_ids:
                raise RuntimeError(f"cannot delete {trace_id}")
            self._db._delete(trace_id)
        else:  # pragma: no cover - unexpected statement
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _FakeConnection:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)


class _FakeMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def increment(self, name: str, labels: dict[str, Any]) -> None:
        self.calls.append((name, labels))


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _store(db: _FakeDB, metrics: _FakeMetrics | None = None) -> PostgresTraceStore:
    settings = cast(Settings, SimpleNamespace())
    return PostgresTraceStore(
        settings,
        connection_factory=lambda: _FakeConnection(db),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# R13.3 — no period configured retains everything
# ---------------------------------------------------------------------------


def test_no_period_retains_everything_without_opening_connection() -> None:
    db = _FakeDB()
    db.add("a" * 32, datetime(2000, 1, 1, tzinfo=timezone.utc), ["s1", "s2"])

    opened = False

    def factory() -> _FakeConnection:
        nonlocal opened
        opened = True
        return _FakeConnection(db)

    settings = cast(Settings, SimpleNamespace())
    store = PostgresTraceStore(settings, connection_factory=factory)

    store.enforce_retention(None)

    assert set(db.traces) == {"a" * 32}  # nothing removed (R13.3)
    assert opened is False  # no-op never touches the database
    assert store.retention_errors == []


# ---------------------------------------------------------------------------
# R13.1 / R13.2 — strictly-older removed (cascade), boundary retained
# ---------------------------------------------------------------------------


def test_removes_strictly_older_cascades_spans_and_retains_boundary(monkeypatch) -> None:
    # Freeze the clock so the store's internal cutoff (now - max_age) matches the
    # trace timestamps exactly. Without this the test is timing-dependent: the
    # store calls datetime.now() a moment after the test does, so a trace placed
    # exactly at the boundary ends up microseconds older than the cutoff and is
    # wrongly removed. (This passes on Windows, whose datetime.now() has ~15ms
    # resolution, but fails on Linux CI where the two now() calls differ.)
    fixed_now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401 - test double
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(trace_store_module, "datetime", _FrozenDateTime)

    max_age = timedelta(hours=24)
    db = _FakeDB()
    db.add("old", fixed_now - timedelta(hours=48), ["s1", "s2"])  # strictly older -> removed
    db.add("boundary", fixed_now - max_age, ["s3"])               # exactly at boundary -> kept
    db.add("recent", fixed_now - timedelta(hours=1), ["s4"])      # within period -> kept

    store = _store(db)
    store.enforce_retention(max_age)

    assert set(db.traces) == {"boundary", "recent"}  # R13.1 (boundary inclusive)
    assert "old" not in db.spans                      # R13.2 cascade
    assert store.retention_errors == []


# ---------------------------------------------------------------------------
# R13.5 — a failed deletion retains that trace and records an error,
#         while the remaining expired traces are still removed
# ---------------------------------------------------------------------------


def test_failed_deletion_retains_trace_records_error_and_removes_others() -> None:
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=24)
    db = _FakeDB()
    db.add("keep_fail", now - timedelta(hours=48), ["s1"])
    db.add("remove_ok", now - timedelta(hours=72), ["s2"])
    db.fail_bulk = True            # force the per-row fallback
    db.fail_ids = {"keep_fail"}    # this row keeps failing

    metrics = _FakeMetrics()
    store = _store(db, metrics)
    store.enforce_retention(max_age)

    # The failing trace (and its spans) is retained intact; the other is removed.
    assert "keep_fail" in db.traces
    assert db.spans["keep_fail"] == ["s1"]
    assert "remove_ok" not in db.traces

    # An error indication identifying the failed removal is recorded.
    assert len(store.retention_errors) == 1
    assert store.retention_errors[0]["trace_id"] == "keep_fail"
    assert "keep_fail" in store.retention_errors[0]["error"]

    # The retention-failure counter was incremented for the failed row.
    assert (
        "rag_trace_store_retention_failures_total",
        {},
    ) in metrics.calls


def test_unenumerable_candidates_record_single_error_and_retain_all() -> None:
    now = datetime.now(timezone.utc)
    db = _FakeDB()
    db.add("t1", now - timedelta(hours=48), ["s1"])
    db.fail_bulk = True
    db.fail_select = True  # cannot even list candidate ids

    store = _store(db)
    store.enforce_retention(timedelta(hours=24))

    assert set(db.traces) == {"t1"}  # everything retained
    assert len(store.retention_errors) == 1
    assert store.retention_errors[0]["trace_id"] is None


def test_successful_bulk_delete_records_no_errors() -> None:
    now = datetime.now(timezone.utc)
    db = _FakeDB()
    db.add("t1", now - timedelta(hours=48), ["s1"])
    db.add("t2", now - timedelta(hours=1), ["s2"])

    metrics = _FakeMetrics()
    store = _store(db, metrics)
    store.enforce_retention(timedelta(hours=24))

    assert set(db.traces) == {"t2"}
    assert store.retention_errors == []
    assert metrics.calls == []  # no failures counted on the happy path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
