"""Property test for atomic trace persistence and commit counting.

Feature: ai-observability-platform.

This module exercises the real
:meth:`rag_system.observability_tracing.trace_store.PostgresTraceStore.persist`
across arbitrary traces and arbitrary injected failure modes, asserting the
design's Property 8 / R5.1, R5.5, R5.6:

* **Atomic** — persistence happens inside a single transaction. If inserting the
  trace row or *any* of its span rows fails, neither the trace nor any of its
  spans remain in the committed store (no partial data survives).
* **Counts only on commit** — the per-route ``rag_traces_persisted_total``
  counter increases by exactly the number of traces whose transaction fully
  commits for that route, and never for a trace whose transaction rolled back.

Rather than the in-memory store *double* (which models the abstract transaction
semantics), this test drives the production store object through an injected
**fake psycopg-style connection** that mirrors a real transaction boundary:

* ``cur.execute(INSERT ...)`` stages a row onto the live transaction;
* exiting the ``with conn:`` block cleanly **commits** the staged rows into the
  shared committed backend (mirroring ``psycopg``'s connection context manager);
* exiting via an exception **rolls back** — the staged rows are discarded and
  the exception propagates so the store's own best-effort handler runs.

Failure is injected exactly where the store performs its inserts (the trace
``INSERT`` or a chosen span ``INSERT``), so the rollback path under test is the
real one: ``PostgresTraceStore._write_atomically`` raising mid-transaction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.observability_tracing.models import Span, Trace
from rag_system.observability_tracing.trace_store import PostgresTraceStore

#: The metric the store increments once per fully-committed trace (R5.6).
_PERSISTED_METRIC = "rag_traces_persisted_total"


# ---------------------------------------------------------------------------
# Fake transactional psycopg-style connection backed by a shared committed store
# ---------------------------------------------------------------------------


class _StoreError(RuntimeError):
    """Injected database failure that forces a mid-transaction rollback."""


@dataclass
class _Backend:
    """The shared *committed* state plus per-trace failure switches.

    ``traces`` / ``spans`` hold only data from transactions that committed
    cleanly, exactly like rows visible in a real database after ``COMMIT``.
    Staged-but-rolled-back rows never reach here.
    """

    #: trace_id -> committed trace insert params.
    traces: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    #: trace_id -> list of committed span insert params.
    spans: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    #: trace_ids whose trace-row INSERT raises (rolls the whole txn back).
    fail_trace_ids: set[str] = field(default_factory=set)
    #: trace_id -> predicate(span_id) selecting the span INSERT that raises.
    fail_span: dict[str, Callable[[str], bool]] = field(default_factory=dict)


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        backend = self._conn.backend
        if "INSERT INTO traces" in sql:
            trace_id = params[0]
            if trace_id in backend.fail_trace_ids:
                raise _StoreError(f"forced trace insert failure for {trace_id!r}")
            self._conn.staged_trace = (trace_id, params)
        elif "INSERT INTO spans" in sql:
            trace_id, span_id = params[0], params[1]
            predicate = backend.fail_span.get(trace_id)
            if predicate is not None and predicate(span_id):
                raise _StoreError(
                    f"forced span insert failure for trace {trace_id!r} span {span_id!r}"
                )
            self._conn.staged_spans.append((trace_id, span_id, params))
        else:  # pragma: no cover - persist only issues the two INSERTs
            raise AssertionError(f"unexpected SQL: {sql}")


class _FakeConnection:
    """A single transaction: stages rows, commits on clean exit, rolls back on error."""

    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.staged_trace: tuple[str, tuple[Any, ...]] | None = None
        self.staged_spans: list[tuple[str, str, tuple[Any, ...]]] = []

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type: object, *_: object) -> bool:
        if exc_type is None:
            self._commit()
        # else: rollback == discard staged rows (do nothing).
        # Return False so any exception propagates to the store's handler.
        return False

    def _commit(self) -> None:
        if self.staged_trace is None:  # pragma: no cover - persist always stages the trace first
            return
        trace_id, trace_params = self.staged_trace
        self.backend.traces[trace_id] = trace_params
        self.backend.spans.setdefault(trace_id, [])
        for staged_trace_id, _span_id, span_params in self.staged_spans:
            self.backend.spans.setdefault(staged_trace_id, []).append(span_params)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


class _FakeMetrics:
    """Records counter increments keyed by ``(name, route)``."""

    def __init__(self) -> None:
        self.counts: Counter[tuple[str, Any]] = Counter()

    def increment(self, name: str, labels: dict[str, Any]) -> None:
        self.counts[(name, labels.get("route"))] += 1


def _store(backend: _Backend, metrics: _FakeMetrics) -> PostgresTraceStore:
    settings = cast(Settings, SimpleNamespace())
    return PostgresTraceStore(
        settings,
        connection_factory=lambda: _FakeConnection(backend),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Smart generators - valid traces plus a per-trace failure mode.
# ---------------------------------------------------------------------------

_STATUSES = st.sampled_from(["success", "error"])
_ROUTES = st.sampled_from(["/ask", "/query", "/copilot", "/hybrid"])
_utc_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(timezone.utc),
)
_durations = st.integers(min_value=0, max_value=10_000_000)
_attr_values = st.one_of(
    st.text(max_size=20),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.booleans(),
)
_attributes = st.dictionaries(
    keys=st.text(min_size=1, max_size=12), values=_attr_values, max_size=4
)


@st.composite
def _trace(draw: st.DrawFn, trace_id: str) -> Trace:
    """A valid Trace with a Root_Span and 0..N resolvable child spans."""
    span_ids = draw(
        st.lists(
            st.text(alphabet="abcdef0123456789", min_size=4, max_size=12),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    spans: list[Span] = []
    for index, span_id in enumerate(span_ids):
        parent = None if index == 0 else draw(st.sampled_from(span_ids[:index]))
        spans.append(
            Span(
                span_id=span_id,
                parent_span_id=parent,
                operation=draw(st.text(min_size=1, max_size=16)),
                start_ts=draw(_utc_datetimes),
                duration_ms=draw(_durations),
                status=draw(_STATUSES),
                attributes=draw(_attributes),
            )
        )
    return Trace(
        trace_id=trace_id,
        route=draw(_ROUTES),
        start_ts=draw(_utc_datetimes),
        duration_ms=draw(_durations),
        root_status=draw(_STATUSES),
        spans=spans,
    )


@dataclass
class _Scenario:
    trace: Trace
    #: One of "ok" (commits), "trace" (trace INSERT fails), "span" (a span
    #: INSERT fails); for "span", ``fail_span_id`` names the failing span.
    fail_mode: str
    fail_span_id: str | None


@st.composite
def _scenarios(draw: st.DrawFn) -> list[_Scenario]:
    """A batch of traces with unique ids, each tagged with a failure mode."""
    count = draw(st.integers(min_value=1, max_value=6))
    # Unique 32-char hex trace ids so committed rows never collide.
    trace_ids = draw(
        st.lists(
            st.text(alphabet="abcdef0123456789", min_size=32, max_size=32),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    scenarios: list[_Scenario] = []
    for trace_id in trace_ids:
        trace = draw(_trace(trace_id))
        fail_mode = draw(st.sampled_from(["ok", "ok", "trace", "span"]))
        fail_span_id: str | None = None
        if fail_mode == "span":
            fail_span_id = draw(st.sampled_from([s.span_id for s in trace.spans]))
        scenarios.append(_Scenario(trace, fail_mode, fail_span_id))
    return scenarios


# ---------------------------------------------------------------------------
# Property 8 - persistence is atomic and counts only on commit.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 8: Trace persistence is atomic and counts only on commit
# Validates: Requirements 5.1, 5.5, 5.6
@settings(max_examples=100)
@given(scenarios=_scenarios())
def test_persistence_is_atomic_and_counts_only_on_commit(
    scenarios: list[_Scenario],
) -> None:
    """For any batch of traces with injected failures, the store persists each
    trace atomically and increments the per-route counter once per committed
    trace only (R5.1, R5.5, R5.6).
    """
    backend = _Backend()
    metrics = _FakeMetrics()
    store = _store(backend, metrics)

    committed_per_route: Counter[str] = Counter()

    for scenario in scenarios:
        trace = scenario.trace
        if scenario.fail_mode == "trace":
            backend.fail_trace_ids.add(trace.trace_id)
        elif scenario.fail_mode == "span":
            target = scenario.fail_span_id
            backend.fail_span[trace.trace_id] = lambda sid, t=target: sid == t

        # persist never raises to the caller, regardless of outcome (R5.4).
        store.persist(trace)

        if scenario.fail_mode == "ok":
            committed_per_route[trace.route] += 1

    # -- Atomicity (R5.1, R5.5) --------------------------------------------
    for scenario in scenarios:
        trace = scenario.trace
        if scenario.fail_mode == "ok":
            # The trace row and every span row are present (nothing dropped).
            assert trace.trace_id in backend.traces
            assert len(backend.spans.get(trace.trace_id, [])) == len(trace.spans)
        else:
            # A failure anywhere in the transaction leaves no partial data:
            # neither the trace row nor any of its span rows survive.
            assert trace.trace_id not in backend.traces
            assert trace.trace_id not in backend.spans

    # -- Counts only on commit (R5.6) --------------------------------------
    all_routes = {s.trace.route for s in scenarios}
    for route in all_routes:
        expected = committed_per_route[route]
        actual = metrics.counts.get((_PERSISTED_METRIC, route), 0)
        assert actual == expected, (
            f"route {route!r}: expected {expected} persisted-trace increments, "
            f"got {actual}"
        )

    # The counter total equals the number of fully-committed traces overall.
    total_persisted = sum(
        count
        for (name, _route), count in metrics.counts.items()
        if name == _PERSISTED_METRIC
    )
    assert total_persisted == sum(committed_per_route.values())


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
