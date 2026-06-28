# Feature: ai-observability-platform, Property 16: Log search rejects invalid range and out-of-range limit
"""Property-based tests for log search request validation (task 14.5).

**Validates: Requirements 16.7, 16.8**

Uses Hypothesis to generate arbitrary inverted timestamp ranges and out-of-range
limit values and asserts the ``GET /logs`` endpoint rejects them with HTTP 400
without ever calling the underlying log store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.observability_tracing.log_store import LogSearchFilters


# ---------------------------------------------------------------------------
# Fake log store that records calls (should never be called on rejected requests)
# ---------------------------------------------------------------------------


class SpyLogStore:
    """A spy log store that records whether search was invoked."""

    def __init__(self) -> None:
        self.search_called = False

    def get_by_trace(self, trace_id: str) -> list:
        return []

    def search(self, filters: LogSearchFilters) -> list:
        self.search_called = True
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(store: SpyLogStore) -> TestClient:
    with patch.object(api_module, "get_log_store", return_value=store):
        return TestClient(api_module.app)


# Strategy: generate a pair of datetimes where end is strictly before start
_aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(timezone.utc),
)


@st.composite
def inverted_time_ranges(draw):
    """Generate (start, end) where end < start (inverted range)."""
    dt1 = draw(_aware_datetimes)
    # Ensure at least 1 second gap so end is strictly before start
    gap = draw(st.timedeltas(min_value=timedelta(seconds=1), max_value=timedelta(days=365)))
    start = dt1 + gap
    end = dt1
    assume(end < start)
    return start, end


# Strategy: generate limit values outside 1..1000
_out_of_range_limits = st.one_of(
    st.integers(max_value=0),           # zero and negative
    st.integers(min_value=1001),        # above maximum
)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(data=inverted_time_ranges())
def test_inverted_time_range_rejected_with_400(data) -> None:
    """**Validates: Requirements 16.7**

    IF a search request supplies an end timestamp earlier than its start
    timestamp, THEN the Log_Query_Service SHALL reject the request with HTTP 400
    indicating the timestamp range is invalid, and never query the store.
    """
    start, end = data
    store = SpyLogStore()

    with patch.object(api_module, "get_log_store", return_value=store):
        client = TestClient(api_module.app)
        response = client.get(
            "/logs",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "range" in detail
    assert not store.search_called


@settings(max_examples=100)
@given(bad_limit=_out_of_range_limits)
def test_out_of_range_limit_rejected_with_400(bad_limit: int) -> None:
    """**Validates: Requirements 16.8**

    IF a search request supplies a result limit outside the range 1 to 1000,
    THEN the Log_Query_Service SHALL reject the request with HTTP 400 indicating
    the limit is out of range, and never query the store.
    """
    store = SpyLogStore()

    with patch.object(api_module, "get_log_store", return_value=store):
        client = TestClient(api_module.app)
        response = client.get("/logs", params={"limit": bad_limit})

    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "limit" in detail
    assert not store.search_called
