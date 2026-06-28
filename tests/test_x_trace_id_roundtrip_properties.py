"""Property test for X-Trace-Id response header round-trip.

Feature: ai-observability-platform, Property 25: X-Trace-Id response header round-trips the request value

This module validates that *for any* valid 32-character lowercase hexadecimal
trace_id sent as the ``X-Trace-Id`` request header, the response includes the
exact same value in its ``X-Trace-Id`` header.

The endpoint is exercised through a FastAPI ``TestClient`` against the real
``app`` in ``rag_system.api``. Simple endpoints (``GET /health`` and ``GET /``)
are used to avoid needing external service mocks — both go through the
``log_requests`` middleware which handles the header propagation.

**Validates: Requirements 11.3**
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system import api as api_module

_client = TestClient(api_module.app)

# ---------------------------------------------------------------------------
# Smart generator — valid 32-character lowercase hex trace_ids.
# ---------------------------------------------------------------------------

_valid_trace_ids = st.text(
    alphabet="0123456789abcdef",
    min_size=32,
    max_size=32,
)

_endpoints = st.sampled_from(["/health", "/"])


# ---------------------------------------------------------------------------
# Property 25 — X-Trace-Id response header round-trips the request value.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 25: X-Trace-Id response header round-trips the request value
# **Validates: Requirements 11.3**
@settings(max_examples=100)
@given(trace_id=_valid_trace_ids, endpoint=_endpoints)
def test_x_trace_id_response_header_round_trips_request_value(
    trace_id: str,
    endpoint: str,
) -> None:
    """WHEN a request includes an X-Trace-Id header with a valid 32-char hex
    value, the response X-Trace-Id header contains the exact same value (R11.3).
    """
    response = _client.get(endpoint, headers={"X-Trace-Id": trace_id})

    # The request should succeed (these are simple no-dependency endpoints).
    assert response.status_code == 200, (
        f"Expected 200 for {endpoint}, got {response.status_code}"
    )

    # The response MUST include the X-Trace-Id header.
    response_trace_id = response.headers.get("x-trace-id")
    assert response_trace_id is not None, (
        f"Response missing X-Trace-Id header for {endpoint}"
    )

    # The response header value MUST exactly match the request value.
    assert response_trace_id == trace_id, (
        f"X-Trace-Id mismatch: sent {trace_id!r}, "
        f"received {response_trace_id!r} on {endpoint}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
