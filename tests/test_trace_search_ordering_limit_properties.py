"""Property test for trace search ordering and result-limit behaviour.

Feature: ai-observability-platform.

This module exercises trace search through the in-memory transactional store
double (``tests/observability_tracing_store_double.py``), the same double the
spec designates for the persistence/query property tests (task 8.2). It isolates
the *ordering and limit* behaviour of ``search_traces`` (design Property 12 /
R8.6, R8.7) from the filtering behaviour validated separately by Property 11
(task 9.6):

- when a search omits a result limit, the service returns at most 100 traces
  ordered by start timestamp in descending order (R8.6);
- when a search supplies a valid result limit (an integer in 1..1000), the
  service returns at most that many traces ordered by start timestamp in
  descending order (R8.7).

To make truncation a routinely exercised path, the number of generated traces
can comfortably exceed the limits under test, so the cap actually bites. Start
timestamps are drawn from a small shared pool so ties are common, ensuring the
descending-by-start-timestamp ordering is checked in the presence of equal
timestamps. No filters are applied, so every stored trace is a candidate and the
returned count is governed purely by the effective limit.
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
# Smart generators - constrained to the ordering/limit domain.
# ---------------------------------------------------------------------------

# Default limit applied when a search omits one (R8.6), and the maximum a client
# may supply (R8.7). Mirrors the store double's _DEFAULT_LIMIT / _MAX_LIMIT.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000

# A small UTC timestamp pool shared by all traces so equal start timestamps are
# common; this forces the descending-by-start-timestamp ordering (R8.6, R8.7) to
# be exercised in the presence of ties rather than only on distinct values.
_BASE_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TS_OFFSETS = (0, 5, 10, 15, 20)
_ts_pool = st.sampled_from([_BASE_TS + timedelta(seconds=o) for o in _TS_OFFSETS])

# A single route/status keeps filtering a no-op so the returned count is governed
# purely by the limit; durations are irrelevant here and pinned to 0.
_ROUTE = "/ask"
_STATUS = "success"


@st.composite
def _traces(draw: st.DrawFn) -> list[Trace]:
    """Build a list of 0..150 traces with unique trace_ids.

    The upper bound comfortably exceeds the default limit (100) and the small
    supplied limits under test, so truncation is a frequently exercised path.
    Spans are irrelevant to search ordering/limit and are omitted; trace_ids are
    assigned by index to guarantee uniqueness within the store.
    """
    count = draw(st.integers(min_value=0, max_value=150))
    return [
        Trace(
            trace_id=f"{index:032x}",
            route=_ROUTE,
            start_ts=draw(_ts_pool),
            duration_ms=0,
            root_status=_STATUS,
            spans=[],
        )
        for index in range(count)
    ]


# A limit is either omitted (None -> the store applies its default of 100, R8.6)
# or a valid client-supplied value in 1..1000 (R8.7). Small values are weighted
# in via the explicit range so truncation against the generated trace volume is
# routinely hit; boundary values 1 and 1000 are included.
_maybe_limit = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=_MAX_LIMIT),
)


def _effective_limit(limit: int | None) -> int:
    """Independent oracle for the limit actually applied.

    Restates R8.6/R8.7: an omitted limit defaults to 100; a supplied limit (in
    the valid 1..1000 range) is honoured as-is.
    """
    if limit is None:
        return _DEFAULT_LIMIT
    return min(max(0, limit), _MAX_LIMIT)


# ---------------------------------------------------------------------------
# Property 12 - trace search ordering and limit are honoured.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 12: Trace search ordering and limit are honoured
# Validates: Requirements 8.6, 8.7
@settings(max_examples=100)
@given(traces=_traces(), limit=_maybe_limit)
def test_trace_search_ordering_and_limit_are_honoured(
    traces: list[Trace], limit: int | None
) -> None:
    """``search_traces`` returns at most the effective limit of traces ordered
    by start timestamp descending.

    Covers the default-limit case (omitted limit -> at most 100, R8.6) and the
    supplied-limit case (1..1000 -> at most that many, R8.7). With no filters
    every stored trace is a candidate, so the returned count must equal
    ``min(total, effective_limit)``; the results must be ordered by start
    timestamp descending; and the returned traces must be the top slice by start
    timestamp (every returned trace's start timestamp is >= every excluded
    trace's start timestamp).
    """
    store = InMemoryTransactionalStore()
    for trace in traces:
        store.persist(trace)

    # Build filters either omitting the limit (exercise the default) or supplying
    # a valid one, leaving all other filters unset so nothing is filtered out.
    if limit is None:
        filters = TraceSearchFilters(route=_ROUTE)
    else:
        filters = TraceSearchFilters(route=_ROUTE, limit=limit)

    results = store.search_traces(filters)

    effective = _effective_limit(limit)

    # R8.6/R8.7: never more than the effective limit.
    assert len(results) <= effective
    # With no filtering, exactly min(total, limit) traces come back.
    assert len(results) == min(len(traces), effective)

    # Ordered by start timestamp in descending order.
    start_timestamps = [trace.start_ts for trace in results]
    assert start_timestamps == sorted(start_timestamps, reverse=True)

    # No trace is returned more than once.
    returned_ids = {trace.trace_id for trace in results}
    assert len(returned_ids) == len(results)

    # The returned traces are the top slice by start timestamp: the smallest
    # returned start timestamp is >= the largest excluded start timestamp. This
    # confirms the limit truncates the *tail* of the descending order, not an
    # arbitrary subset.
    if results and len(results) < len(traces):
        excluded = [t for t in traces if t.trace_id not in returned_ids]
        assert min(start_timestamps) >= max(t.start_ts for t in excluded)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
