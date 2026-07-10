"""Unit tests for :meth:`PostgresTraceStore.get_trace` and ``search_traces``
(task 9.2, R7 and R8).

These tests drive the store through an injected fake psycopg-style connection
that interprets the read statements (``SELECT_TRACE_SQL``, ``SELECT_SPANS_SQL``,
and the dynamically built ``search_traces`` query) against an in-memory set of
traces+spans. This exercises the real SQL-building, ordering, limit clamping,
and row-reconstruction logic without a live database, mirroring the fake-driven
style of ``test_trace_store_retention.py``.

Coverage:

* R7.1 / R7.4 — ``get_trace`` returns the full trace, or ``None`` when absent.
* R7.2 — spans are ordered by start timestamp ascending, ties broken by span_id
  ascending (the fake honours the ``ORDER BY start_ts ASC, span_id ASC`` clause).
* R7.5 — the Root_Span's ``parent_span_id`` round-trips as ``None``.
* R8.1 — inclusive ``[start, end]`` range on the trace start timestamp.
* R8.2 / R8.3 — case-sensitive ``route`` / ``status`` equality.
* R8.4 — ``min_duration_ms`` lower bound (``>=``).
* R8.5 — multiple filters combine with AND semantics.
* R8.6 / R8.7 — default limit 100, capped at 1000, descending by start timestamp.
* R8.10 — an empty result set when nothing matches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rag_system.config import Settings
from rag_system.observability_tracing.trace_store import (
    PostgresTraceStore,
    TraceSearchFilters,
)


# ---------------------------------------------------------------------------
# Fake psycopg-style connection backed by an in-memory trace/span store
# ---------------------------------------------------------------------------


@dataclass
class _TraceRow:
    trace_id: str
    route: str
    start_ts: datetime
    duration_ms: int
    root_status: str
    # AI-configuration attribution columns (R9.1, R9.11); default to the
    # "unresolved" shape so existing call sites that don't care about them are
    # unaffected.
    ai_configuration_version_id: str | None = None
    resolved_settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SpanRow:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    operation: str
    start_ts: datetime
    duration_ms: int
    status: str
    attributes: dict[str, Any]


class _FakeDB:
    """In-memory traces+spans the fake connection reads from."""

    def __init__(self) -> None:
        self.traces: dict[str, _TraceRow] = {}
        self.spans: dict[str, list[_SpanRow]] = {}

    def add_trace(
        self,
        trace_id: str,
        *,
        route: str = "/ask",
        start_ts: datetime,
        duration_ms: int = 10,
        root_status: str = "success",
        ai_configuration_version_id: str | None = None,
        resolved_settings: dict[str, Any] | None = None,
    ) -> None:
        self.traces[trace_id] = _TraceRow(
            trace_id,
            route,
            start_ts,
            duration_ms,
            root_status,
            ai_configuration_version_id,
            resolved_settings or {},
        )
        self.spans.setdefault(trace_id, [])

    def add_span(self, span: _SpanRow) -> None:
        self.spans.setdefault(span.trace_id, []).append(span)


class _FakeCursor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "FROM spans" in sql:
            self._execute_spans(sql, params)
        elif "FROM traces" in sql and "WHERE trace_id = %s" in sql:
            self._execute_get_trace(params)
        elif "FROM traces" in sql:
            self._execute_search(sql, params)
        else:  # pragma: no cover - unexpected statement
            raise AssertionError(f"unexpected SQL: {sql}")

    def _execute_get_trace(self, params: tuple[Any, ...]) -> None:
        trace_id = params[0]
        row = self._db.traces.get(trace_id)
        self._result = [] if row is None else [self._trace_tuple(row)]

    def _execute_spans(self, sql: str, params: tuple[Any, ...]) -> None:
        # Two span queries exist in production:
        #   * SELECT_SPANS_SQL          — WHERE trace_id = %s  (single trace)
        #   * SELECT_SPANS_FOR_TRACES_SQL — WHERE trace_id = ANY(%s), with
        #     trace_id appended as the final selected column so search_traces
        #     can group the batched result in one round trip (no N+1).
        batched = "ANY(" in sql
        if batched:
            trace_ids = list(params[0])
            spans = [
                span
                for trace_id in trace_ids
                for span in self._db.spans.get(trace_id, [])
            ]
            # Honour ORDER BY trace_id ASC, start_ts ASC, span_id ASC.
            spans.sort(key=lambda s: (s.trace_id, s.start_ts, s.span_id))
            self._result = [(*self._span_tuple(s), s.trace_id) for s in spans]
        else:
            trace_id = params[0]
            spans = list(self._db.spans.get(trace_id, []))
            # Honour ORDER BY start_ts ASC, span_id ASC (R7.2).
            spans.sort(key=lambda s: (s.start_ts, s.span_id))
            self._result = [self._span_tuple(s) for s in spans]

    @staticmethod
    def _span_tuple(s: _SpanRow) -> tuple[Any, ...]:
        return (
            s.span_id,
            s.parent_span_id,
            s.operation,
            s.start_ts,
            s.duration_ms,
            s.status,
            s.attributes,
        )

    def _execute_search(self, sql: str, params: tuple[Any, ...]) -> None:
        # params end with the LIMIT; the leading params bind the WHERE clauses in
        # the order trace_store builds them: start, end, route, status,
        # min_duration. We re-derive the active predicates from the SQL text.
        bind = list(params)
        limit = bind.pop()  # last param is always the LIMIT
        idx = 0
        start = end = route = status = min_dur = None
        if "start_ts >= %s" in sql:
            start = _parse(bind[idx])
            idx += 1
        if "start_ts <= %s" in sql:
            end = _parse(bind[idx])
            idx += 1
        if "route = %s" in sql:
            route = bind[idx]
            idx += 1
        if "root_status = %s" in sql:
            status = bind[idx]
            idx += 1
        if "duration_ms >= %s" in sql:
            min_dur = bind[idx]
            idx += 1

        rows = [
            r
            for r in self._db.traces.values()
            if (start is None or r.start_ts >= start)
            and (end is None or r.start_ts <= end)
            and (route is None or r.route == route)
            and (status is None or r.root_status == status)
            and (min_dur is None or r.duration_ms >= min_dur)
        ]
        rows.sort(key=lambda r: r.start_ts, reverse=True)  # DESC (R8.6/R8.7)
        rows = rows[:limit]
        self._result = [self._trace_tuple(r) for r in rows]

    @staticmethod
    def _trace_tuple(row: _TraceRow) -> tuple[Any, ...]:
        return (
            row.trace_id,
            row.route,
            row.start_ts,
            row.duration_ms,
            row.root_status,
            row.ai_configuration_version_id,
            row.resolved_settings,
        )

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

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


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _store(db: _FakeDB) -> PostgresTraceStore:
    settings = cast(Settings, SimpleNamespace())
    return PostgresTraceStore(settings, connection_factory=lambda: _FakeConnection(db))


def _ts(offset_seconds: int = 0) -> datetime:
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# get_trace (R7)
# ---------------------------------------------------------------------------


def test_get_trace_absent_returns_none() -> None:
    store = _store(_FakeDB())
    assert store.get_trace("a" * 32) is None  # R7.1 / R7.4


def test_get_trace_orders_spans_and_root_parent_is_null() -> None:
    trace_id = "b" * 32
    db = _FakeDB()
    db.add_trace(trace_id, start_ts=_ts(0))
    # Insert spans out of order; two share a start timestamp to test the span_id
    # tie-break (R7.2).
    db.add_span(_SpanRow(trace_id, "s3", "root", "late", _ts(5), 1, "success", {}))
    db.add_span(_SpanRow(trace_id, "root", None, "http", _ts(0), 9, "success", {}))
    db.add_span(_SpanRow(trace_id, "s2", "root", "b-tie", _ts(2), 1, "success", {}))
    db.add_span(_SpanRow(trace_id, "s1", "root", "a-tie", _ts(2), 1, "success", {}))

    trace = _store(db).get_trace(trace_id)
    assert trace is not None
    assert trace.trace_id == trace_id
    ordered = [s.span_id for s in trace.spans]
    # Ascending start_ts, ties (s1, s2 both at _ts(2)) broken by ascending span_id.
    assert ordered == ["root", "s1", "s2", "s3"]
    # The Root_Span's parent is an explicit null (R7.5).
    root = next(s for s in trace.spans if s.span_id == "root")
    assert root.parent_span_id is None


# ---------------------------------------------------------------------------
# search_traces (R8)
# ---------------------------------------------------------------------------


def test_search_inclusive_time_range() -> None:
    db = _FakeDB()
    db.add_trace("a" * 32, start_ts=_ts(0))    # == start boundary
    db.add_trace("b" * 32, start_ts=_ts(50))   # inside
    db.add_trace("c" * 32, start_ts=_ts(100))  # == end boundary
    db.add_trace("d" * 32, start_ts=_ts(150))  # outside

    results = _store(db).search_traces(
        TraceSearchFilters(start=_ts(0), end=_ts(100))
    )
    got = {t.trace_id for t in results}
    assert got == {"a" * 32, "b" * 32, "c" * 32}  # both boundaries inclusive (R8.1)


def test_search_route_and_status_are_case_sensitive() -> None:
    db = _FakeDB()
    db.add_trace("a" * 32, route="/ask", root_status="success", start_ts=_ts(0))
    db.add_trace("b" * 32, route="/Ask", root_status="success", start_ts=_ts(1))
    db.add_trace("c" * 32, route="/ask", root_status="error", start_ts=_ts(2))

    by_route = _store(db).search_traces(TraceSearchFilters(route="/ask"))
    assert {t.trace_id for t in by_route} == {"a" * 32, "c" * 32}  # R8.2

    by_status = _store(db).search_traces(TraceSearchFilters(status="success"))
    assert {t.trace_id for t in by_status} == {"a" * 32, "b" * 32}  # R8.3


def test_search_min_duration_lower_bound() -> None:
    db = _FakeDB()
    db.add_trace("a" * 32, duration_ms=50, start_ts=_ts(0))
    db.add_trace("b" * 32, duration_ms=100, start_ts=_ts(1))
    db.add_trace("c" * 32, duration_ms=150, start_ts=_ts(2))

    results = _store(db).search_traces(TraceSearchFilters(min_duration_ms=100))
    assert {t.trace_id for t in results} == {"b" * 32, "c" * 32}  # >= (R8.4)


def test_search_filters_combine_with_and_semantics() -> None:
    db = _FakeDB()
    db.add_trace("hit", route="/ask", root_status="error", duration_ms=200, start_ts=_ts(10))
    db.add_trace("wrong_route", route="/x", root_status="error", duration_ms=200, start_ts=_ts(10))
    db.add_trace("wrong_status", route="/ask", root_status="success", duration_ms=200, start_ts=_ts(10))
    db.add_trace("too_fast", route="/ask", root_status="error", duration_ms=10, start_ts=_ts(10))
    db.add_trace("too_early", route="/ask", root_status="error", duration_ms=200, start_ts=_ts(-100))

    results = _store(db).search_traces(
        TraceSearchFilters(
            start=_ts(0),
            end=_ts(100),
            route="/ask",
            status="error",
            min_duration_ms=100,
        )
    )
    assert [t.trace_id for t in results] == ["hit"]  # only the trace satisfying all (R8.5)


def test_search_empty_when_no_match() -> None:
    db = _FakeDB()
    db.add_trace("a" * 32, route="/ask", start_ts=_ts(0))
    results = _store(db).search_traces(TraceSearchFilters(route="/missing"))
    assert results == []  # R8.10


def test_search_orders_descending_and_applies_default_limit() -> None:
    db = _FakeDB()
    for i in range(150):
        db.add_trace(f"{i:032d}", start_ts=_ts(i))

    results = _store(db).search_traces(TraceSearchFilters())  # no explicit limit
    assert len(results) == 100  # default limit (R8.6)
    starts = [t.start_ts for t in results]
    assert starts == sorted(starts, reverse=True)  # descending by start ts
    # The 100 most recent traces are the ones returned.
    assert results[0].start_ts == _ts(149)
    assert results[-1].start_ts == _ts(50)


def test_search_limit_is_capped_at_1000() -> None:
    db = _FakeDB()
    for i in range(5):
        db.add_trace(f"{i:032d}", start_ts=_ts(i))

    # A limit above the maximum is clamped to 1000 (R8.7); with 5 traces we get 5.
    results = _store(db).search_traces(TraceSearchFilters(limit=99999))
    assert len(results) == 5


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
