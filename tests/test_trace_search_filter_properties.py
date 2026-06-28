"""Property test for trace search returning exactly the traces satisfying every
filter.

Feature: ai-observability-platform.

This module exercises trace search through the in-memory transactional store
double (``tests/observability_tracing_store_double.py``), the same double the
spec designates for the persistence/query property tests (task 8.2). For any set
of persisted traces and any combination of time-range, route, status, and
minimum-duration filters, ``search_traces`` must return **exactly** the set of
traces that satisfy every supplied filter simultaneously, per the design's
Property 11 / R8.1-R8.5:

- start timestamp within the inclusive ``[start, end]`` range (R8.1);
- route exactly equal under a case-sensitive comparison (R8.2);
- root status exactly equal under a case-sensitive comparison (R8.3);
- total duration greater than or equal to the minimum (R8.4);
- all supplied filters combined with AND semantics (R8.5).

This property isolates the *filtering* behaviour from ordering/limit (Property
12, task 9.7): every search runs with a result limit large enough (1000, the
store's maximum) that no match is ever truncated, so the returned set can be
compared for set equality against an independent oracle that re-applies the same
predicates. To exercise case-sensitivity, routes and statuses are drawn from
pools that include case variants (e.g. ``"/ask"`` vs ``"/Ask"``,
``"success"`` vs ``"Success"``), and filter values are drawn from those same
pools plus values that match nothing.

Start timestamps and filter bounds are drawn from a small shared offset pool so
range filters frequently bisect the data (rather than trivially including or
excluding everything), making the inclusive-boundary behaviour a routinely
exercised part of the property.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import Trace

# Test-support module (not collected by pytest); imported by module name, the
# repo's convention for shared test fakes (see test_preservation_properties.py).
from observability_tracing_store_double import (
    InMemoryTransactionalStore,
    TraceSearchFilters,
)

# ---------------------------------------------------------------------------
# Smart generators - constrained to the trace-search domain.
# ---------------------------------------------------------------------------

# A small UTC timestamp pool shared by trace start times and filter bounds, so
# range filters routinely bisect the data and the inclusive boundaries (R8.1)
# are frequently hit rather than trivially satisfied.
_BASE_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TS_OFFSETS = (0, 10, 20, 30, 40, 50)
_ts_pool = st.sampled_from([_BASE_TS + timedelta(seconds=o) for o in _TS_OFFSETS])

# Route / status pools include case variants so the case-sensitive comparison
# (R8.2, R8.3) is meaningfully exercised: "/ask" must not match "/Ask", and
# "success" must not match "Success".
_ROUTE_POOL = ["/ask", "/Ask", "/query", "/QUERY", "/x"]
_STATUS_POOL = ["success", "error", "Success", "ERROR"]

_routes = st.sampled_from(_ROUTE_POOL)
_statuses = st.sampled_from(_STATUS_POOL)

# Durations span a range wide enough for the min-duration filter to partition
# the data; bounded well under the R8.4 ceiling of 86,400,000 ms.
_DURATIONS = st.integers(min_value=0, max_value=5000)

# 32-char lowercase hex trace_id (the domain's required identifier shape).
_span_hex = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)


@st.composite
def _traces(draw: st.DrawFn) -> list[Trace]:
    """Build a list of 0..N traces with unique trace_ids.

    Spans are irrelevant to search filtering (which reads only trace-level
    fields), so traces are generated span-free. trace_ids are assigned by index
    to guarantee uniqueness within the store, mirroring how a real store keys
    traces by id.
    """
    count = draw(st.integers(min_value=0, max_value=25))
    traces: list[Trace] = []
    for index in range(count):
        traces.append(
            Trace(
                trace_id=f"{index:032x}",
                route=draw(_routes),
                start_ts=draw(_ts_pool),
                duration_ms=draw(_DURATIONS),
                root_status=draw(_statuses),
                spans=[],
            )
        )
    return traces


# Filter component generators: each filter is independently present or absent so
# every combination (including the no-filter case) is reachable.
_maybe_ts = st.one_of(st.none(), _ts_pool)
# Route/status filter values include the in-use pools plus a value that matches
# nothing, so both "matches some" and "matches none" paths are covered.
_maybe_route = st.one_of(st.none(), st.sampled_from([*_ROUTE_POOL, "/none"]))
_maybe_status = st.one_of(st.none(), st.sampled_from([*_STATUS_POOL, "unknown"]))
_maybe_min_duration = st.one_of(st.none(), st.integers(min_value=0, max_value=5000))


@st.composite
def _filters(draw: st.DrawFn) -> TraceSearchFilters:
    """Build a search filter set with each filter independently present/absent.

    The limit is pinned to 1000 (the store maximum) so filtering is never masked
    by truncation; ordering and limit are validated separately by Property 12.
    """
    return TraceSearchFilters(
        start=draw(_maybe_ts),
        end=draw(_maybe_ts),
        route=draw(_maybe_route),
        status=draw(_maybe_status),
        min_duration_ms=draw(_maybe_min_duration),
        limit=1000,
    )


def _satisfies(trace: Trace, filters: TraceSearchFilters) -> bool:
    """Independent oracle: True iff *trace* satisfies every supplied filter.

    Re-states R8.1-R8.4 directly (inclusive range, case-sensitive route/status
    equality, ``>=`` minimum duration) combined with AND semantics (R8.5). A
    ``None`` filter component imposes no constraint.
    """
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


# ---------------------------------------------------------------------------
# Property 11 - trace search returns exactly the traces satisfying every filter.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 11: Trace search returns exactly the traces satisfying every filter
# Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5
@settings(max_examples=100)
@given(traces=_traces(), filters=_filters())
def test_trace_search_returns_exactly_matching_traces(
    traces: list[Trace], filters: TraceSearchFilters
) -> None:
    """``search_traces`` returns exactly the traces satisfying every filter.

    With a limit large enough that no match is truncated, the returned set of
    trace_ids must equal the set computed by an independent oracle applying the
    inclusive time range (R8.1), case-sensitive route (R8.2) and status (R8.3)
    equality, the ``>=`` minimum-duration bound (R8.4), all combined with AND
    semantics (R8.5).
    """
    store = InMemoryTransactionalStore()
    for trace in traces:
        store.persist(trace)

    results = store.search_traces(filters)

    expected_ids = {t.trace_id for t in traces if _satisfies(t, filters)}
    returned_ids = {t.trace_id for t in results}

    # Exactly the matching traces: none missing, none extra.
    assert returned_ids == expected_ids
    # No trace is returned more than once.
    assert len(results) == len(returned_ids)
    # Every returned trace genuinely satisfies every supplied filter (AND).
    for trace in results:
        assert _satisfies(trace, filters)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
