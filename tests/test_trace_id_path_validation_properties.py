"""Property test for trace_id path-parameter validation on the query endpoints.

Feature: ai-observability-platform.

This module validates the design's Property 10 (R7.3, R15.3): *for any* string
supplied as the ``trace_id`` path parameter to the trace-by-id endpoint
(``GET /traces/{trace_id}``) or the logs-by-trace-id endpoint
(``GET /logs/{trace_id}``) that is **not** a 32-character lowercase hexadecimal
string, the service rejects the request with HTTP 400 and returns no trace or
log data.

The endpoints are exercised through a FastAPI ``TestClient`` against the real
``app`` in ``rag_system.api``. The underlying trace and log stores are replaced
with spy doubles that record every call and would return concrete data if ever
invoked, so the test can assert two things at once for a rejected id:

1. the response status is 400, and
2. the store was never queried (hence no trace/log data could be returned).

Generators are constrained to the *non-conforming* input space: non-empty
strings drawn from an alphabet of hex digits, uppercase hex letters, non-hex
letters, and ``-``/``_`` that are not also a valid 32-char lowercase hex id
(the rare valid draw is filtered out). The alphabet deliberately excludes path
separators, ``.``, and whitespace so each generated value is a single clean URL
path segment rather than something the HTTP client would re-route or normalise.
Explicit examples pin down the canonical rejection cases: wrong length, an
uppercase digit, and a non-hex character.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system import api as api_module
from rag_system.api import _TRACE_ID_RE
from rag_system.observability_tracing.models import LogRecordModel, Span, Trace

# ---------------------------------------------------------------------------
# Spy store doubles - record every call and would return data if invoked.
# ---------------------------------------------------------------------------

_VALID_TRACE_ID = "0123456789abcdef0123456789abcdef"


def _canned_trace() -> Trace:
    """A non-empty trace the spy would hand back if it were ever queried."""
    return Trace(
        trace_id=_VALID_TRACE_ID,
        route="/ask",
        start_ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        duration_ms=5,
        root_status="success",
        spans=[
            Span(
                span_id="aaaa",
                parent_span_id=None,
                operation="root",
                start_ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                duration_ms=5,
                status="success",
                attributes={},
            )
        ],
    )


def _canned_log() -> LogRecordModel:
    return LogRecordModel(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        level="INFO",
        logger="rag_system.test",
        message="hello",
        trace_id=_VALID_TRACE_ID,
        exc_text=None,
        extra={},
        insertion_seq=1,
    )


class _SpyTraceStore:
    """Records get_trace calls; returns a concrete trace if ever called."""

    def __init__(self) -> None:
        self.get_trace_calls: list[str] = []

    def get_trace(self, trace_id: str) -> Trace:
        self.get_trace_calls.append(trace_id)
        return _canned_trace()


class _SpyLogStore:
    """Records get_by_trace calls; returns a concrete record if ever called."""

    def __init__(self) -> None:
        self.get_by_trace_calls: list[str] = []

    def get_by_trace(self, trace_id: str) -> list[LogRecordModel]:
        self.get_by_trace_calls.append(trace_id)
        return [_canned_log()]


# A single client is enough; the endpoints resolve the store via the module-level
# ``get_trace_store`` / ``get_log_store`` functions at request time, so patching
# those per example controls which store the request sees.
_client = TestClient(api_module.app)


# ---------------------------------------------------------------------------
# Smart generator - the non-conforming trace_id input space.
# ---------------------------------------------------------------------------

# Hex digits, uppercase hex (case violates "lowercase"), non-hex letters, and a
# couple of URL-safe separators. Excludes '/', '.', '?', '#', and whitespace so
# every value is a single clean path segment the HTTP client passes through
# verbatim without re-routing or path normalisation.
_NON_CONFORMING_ALPHABET = (
    "0123456789abcdef"            # lowercase hex (length is what makes these invalid)
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # uppercase letters - never valid (lowercase required)
    "ghijklmnopqrstuvwxyz"        # non-hex lowercase letters
    "-_"
)

_non_conforming_trace_ids = st.text(
    alphabet=_NON_CONFORMING_ALPHABET, min_size=1, max_size=48
).filter(lambda s: _TRACE_ID_RE.fullmatch(s) is None)


# ---------------------------------------------------------------------------
# Property 10 - trace_id path validation rejects non-conforming identifiers.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 10: trace_id path validation rejects non-conforming identifiers
# Validates: Requirements 7.3, 15.3
@settings(max_examples=100)
@given(bad_trace_id=_non_conforming_trace_ids)
@example(bad_trace_id="0123456789abcdef0123456789abcde")  # 31 chars - too short
@example(bad_trace_id="0123456789abcdef0123456789abcdef0")  # 33 chars - too long
@example(bad_trace_id="0123456789ABCDEF0123456789abcdef")  # uppercase hex digits
@example(bad_trace_id="0123456789abcdeg0123456789abcdef")  # 'g' is not a hex digit
def test_trace_id_path_validation_rejects_non_conforming_identifiers(
    bad_trace_id: str,
) -> None:
    """Both ``GET /traces/{id}`` and ``GET /logs/{id}`` reject a non-conforming
    trace_id with HTTP 400 and never reach the store (R7.3, R15.3)."""
    # Guard: the generated value must genuinely be non-conforming.
    assert _TRACE_ID_RE.fullmatch(bad_trace_id) is None

    spy_trace = _SpyTraceStore()
    spy_log = _SpyLogStore()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api_module, "get_trace_store", lambda: spy_trace)
        mp.setattr(api_module, "get_log_store", lambda: spy_log)

        trace_response = _client.get(f"/traces/{bad_trace_id}")
        log_response = _client.get(f"/logs/{bad_trace_id}")

    # R7.3 - the trace-by-id endpoint rejects with 400 and returns no trace data.
    assert trace_response.status_code == 400
    assert spy_trace.get_trace_calls == []
    trace_body = trace_response.json()
    assert "trace_id" in trace_body["detail"]
    assert "spans" not in trace_body  # error payload, not a Trace

    # R15.3 - the logs-by-trace-id endpoint rejects with 400 and returns no logs.
    assert log_response.status_code == 400
    assert spy_log.get_by_trace_calls == []
    log_body = log_response.json()
    assert "trace_id" in log_body["detail"]
    assert not isinstance(log_body, list)  # error payload, not a log list


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
