"""Unit tests for X-Trace-Id generation path (task 15.3).

Validates: Requirements 11.4

IF a request is received without an `X-Trace-Id` header, THEN THE Tracing_Platform
SHALL generate a trace_id for the request and set it in the `X-Trace-Id` response
header.

Tests exercise the `log_requests` middleware in `rag_system.api` by sending
requests to the `/health` endpoint WITHOUT an `X-Trace-Id` request header and
verifying:

1. The response includes an `X-Trace-Id` header.
2. The generated trace_id matches the expected format (valid UUID4 hex string).
3. Multiple requests without the header get different trace_ids (unique generation).
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from rag_system import api as api_module

#: The middleware generates trace_ids via str(uuid.uuid4()) which produces a
#: hyphenated UUID4 string. Once hyphens are stripped it yields 32 lowercase
#: hex characters.
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _client() -> TestClient:
    return TestClient(api_module.app)


# -- 1. Response includes X-Trace-Id header ---------------------------------


def test_response_includes_x_trace_id_header_when_not_supplied() -> None:
    """A request without X-Trace-Id gets a generated trace_id in the response."""
    client = _client()

    response = client.get("/health")

    assert response.status_code == 200
    assert "x-trace-id" in response.headers


# -- 2. Generated trace_id matches the expected format ----------------------


def test_generated_trace_id_is_valid_uuid4_hex() -> None:
    """The middleware generates a trace_id that is a valid UUID4 hex string."""
    client = _client()

    response = client.get("/health")

    trace_id = response.headers["x-trace-id"]
    # str(uuid.uuid4()) produces a hyphenated UUID4 whose hex content is 32 chars
    assert _UUID4_RE.fullmatch(trace_id), (
        f"Generated trace_id '{trace_id}' does not match expected UUID4 format"
    )
    # Stripping hyphens yields exactly 32 lowercase hex characters
    hex_only = trace_id.replace("-", "")
    assert len(hex_only) == 32
    assert all(c in "0123456789abcdef" for c in hex_only)


# -- 3. Multiple requests get different trace_ids (uniqueness) ---------------


def test_multiple_requests_get_unique_trace_ids() -> None:
    """Each request without X-Trace-Id gets a distinct generated trace_id."""
    client = _client()

    trace_ids = set()
    num_requests = 10

    for _ in range(num_requests):
        response = client.get("/health")
        assert response.status_code == 200
        trace_id = response.headers["x-trace-id"]
        trace_ids.add(trace_id)

    # All generated trace_ids must be unique
    assert len(trace_ids) == num_requests, (
        f"Expected {num_requests} unique trace_ids but got {len(trace_ids)}"
    )
