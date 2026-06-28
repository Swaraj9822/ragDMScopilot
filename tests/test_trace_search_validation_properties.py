"""Property test for trace search request validation on GET /traces.

Feature: ai-observability-platform.

This module validates the design's Property 13 (R8.8, R8.9): *for any* search
request that supplies an end timestamp earlier than its start timestamp, or a
result limit outside the range 1 to 1000, or a minimum-duration value outside
the range 0 to 86,400,000, the Trace_Query_Service rejects the request with
HTTP 400 and never invokes the trace store.

The endpoint is exercised through a FastAPI ``TestClient`` against the real
``app`` in ``rag_system.api``. The underlying trace store is replaced with a
spy double that records every call so the test can assert two things at once:

1. the response status is 400, and
2. the store's ``search_traces`` was never called (hence no trace data could be
   returned).

Generators are constrained to the invalid input space for each parameter:
inverted time ranges, limits outside [1, 1000], and min_duration_ms values
outside [0, 86_400_000].
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system import api as api_module
from rag_system.observability_tracing.trace_store import TraceSearchFilters

# ---------------------------------------------------------------------------
# Spy store double - records every search_traces call.
# ---------------------------------------------------------------------------


class _SpyTraceStore:
    """Records search_traces calls; would return results if ever queried."""

    def __init__(self) -> None:
        self.search_traces_calls: list[TraceSearchFilters] = []

    def search_traces(self, filters: TraceSearchFilters) -> list:
        self.search_traces_calls.append(filters)
        return []


_client = TestClient(api_module.app)


# ---------------------------------------------------------------------------
# Smart generators for the invalid input spaces.
# ---------------------------------------------------------------------------

# Timestamps: generate a start and an end such that end < start (inverted).
_timestamps = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(timezone.utc),
)

_inverted_ranges = st.tuples(_timestamps, _timestamps).filter(
    lambda pair: pair[1] < pair[0]
)

# Out-of-range limits: anything outside [1, 1000].
_bad_limits = st.one_of(
    st.integers(max_value=0),           # zero and negative
    st.integers(min_value=1001),        # above 1000
).filter(lambda x: abs(x) < 10**15)    # keep serializable

# Out-of-range min_duration_ms: anything outside [0, 86_400_000].
_bad_min_durations = st.one_of(
    st.integers(max_value=-1),           # negative
    st.integers(min_value=86_400_001),   # above 86,400,000
).filter(lambda x: abs(x) < 10**15)     # keep serializable


# ---------------------------------------------------------------------------
# Property 13 - Trace search rejects invalid range and out-of-range parameters.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 13: Trace search rejects invalid range and out-of-range parameters
# **Validates: Requirements 8.8, 8.9**
@settings(max_examples=100)
@given(
    scenario=st.sampled_from(["inverted_range", "bad_limit", "bad_min_duration"]),
    inverted_range=_inverted_ranges,
    bad_limit=_bad_limits,
    bad_min_duration=_bad_min_durations,
)
def test_trace_search_rejects_invalid_range_and_out_of_range_parameters(
    scenario: str,
    inverted_range: tuple[datetime, datetime],
    bad_limit: int,
    bad_min_duration: int,
) -> None:
    """GET /traces rejects inverted time ranges and out-of-range limit or
    min_duration_ms with HTTP 400 and never queries the store (R8.8, R8.9)."""
    params: dict[str, str] = {}

    if scenario == "inverted_range":
        start, end = inverted_range
        params["start"] = start.isoformat()
        params["end"] = end.isoformat()
    elif scenario == "bad_limit":
        params["limit"] = str(bad_limit)
    else:  # bad_min_duration
        params["min_duration_ms"] = str(bad_min_duration)

    spy = _SpyTraceStore()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api_module, "get_trace_store", lambda: spy)
        response = _client.get("/traces", params=params)

    # The request MUST be rejected with 400.
    assert response.status_code == 400, (
        f"Expected 400 for scenario={scenario}, params={params}, "
        f"got {response.status_code}"
    )

    # The trace store MUST NOT have been called.
    assert spy.search_traces_calls == [], (
        f"Store was queried despite invalid params: scenario={scenario}"
    )

    # The error body should identify the offending parameter.
    detail = response.json()["detail"].lower()
    if scenario == "inverted_range":
        assert "range" in detail
    elif scenario == "bad_limit":
        assert "limit" in detail
    else:
        assert "min_duration_ms" in detail


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
