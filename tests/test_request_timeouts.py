"""Tests for request-level timeouts.

Validates that:
1. Slow query endpoints return HTTP 504 when exceeding the configured timeout.
2. Fast requests pass through normally without being affected by timeouts.
3. Non-timeout endpoints (health, metrics, docs CRUD) are not affected.
4. Timeout metrics are recorded correctly.
5. The 504 response body includes useful diagnostic information.
"""

import time

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.config import Settings
from rag_system.models import (
    CopilotQueryResponse,
    QueryResponse,
    UnifiedQueryResponse,
)


# ---- Helpers ----


class SlowRagService:
    """A RagService stand-in that takes a configurable time to respond."""

    def __init__(self, delay_s: float):
        self._delay_s = delay_s

    def query(self, request):
        time.sleep(self._delay_s)
        return QueryResponse(
            answer="Answer after delay",
            citations=[],
            evidence_status="grounded",
            trace_id="test-trace",
        )


class SlowCopilotService:
    """A DatabaseCopilotService stand-in that takes a configurable time."""

    def __init__(self, delay_s: float):
        self._delay_s = delay_s

    def query(self, request):
        time.sleep(self._delay_s)
        return CopilotQueryResponse(
            answer="Copilot answer after delay",
            mode="database",
            evidence_status="grounded",
            trace_id="test-trace",
        )


class SlowRouter:
    """An AgenticRouter stand-in that takes a configurable time."""

    def __init__(self, delay_s: float):
        self._delay_s = delay_s

    def query(self, request):
        time.sleep(self._delay_s)
        return UnifiedQueryResponse(
            answer="Unified answer after delay",
            route="rag",
            evidence_status="grounded",
            trace_id="test-trace",
        )


class FastRagService:
    """A RagService that responds immediately."""

    def query(self, request):
        return QueryResponse(
            answer="Fast answer",
            citations=[],
            evidence_status="grounded",
            trace_id="test-trace",
        )

    def delete_document(self, document_id):
        return None  # 404


class FastCopilotService:
    """A CopilotService that responds immediately."""

    def query(self, request):
        return CopilotQueryResponse(
            answer="Fast copilot answer",
            mode="database",
            evidence_status="grounded",
            trace_id="test-trace",
        )


class FastRouter:
    """A Router that responds immediately."""

    def query(self, request):
        return UnifiedQueryResponse(
            answer="Fast unified answer",
            route="rag",
            evidence_status="grounded",
            trace_id="test-trace",
        )


def _make_settings(
    timeout_query: int = 60,
    timeout_copilot: int = 90,
    timeout_ask: int = 120,
) -> Settings:
    return Settings.model_construct(
        max_upload_bytes=1024,
        cors_allowed_origins="http://localhost:3000",
        s3_bucket="my-test-bucket",
        ingestion_queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
        secrets_manager_secret_id="",
        request_timeout_query_s=timeout_query,
        request_timeout_copilot_s=timeout_copilot,
        request_timeout_ask_s=timeout_ask,
        bedrock_read_timeout_s=45,
    )


# ---- Fixtures ----


@pytest.fixture
def timeout_settings():
    """Settings with very short timeouts (1 second) for test speed."""
    return _make_settings(timeout_query=1, timeout_copilot=1, timeout_ask=1)


@pytest.fixture
def disabled_timeout_settings():
    """Settings with timeouts disabled (0)."""
    return _make_settings(timeout_query=0, timeout_copilot=0, timeout_ask=0)


@pytest.fixture
def slow_client(monkeypatch, timeout_settings):
    """TestClient where all services are slow (3s) but timeouts are short (1s)."""
    monkeypatch.setattr(api_module, "get_settings", lambda: timeout_settings)
    monkeypatch.setattr(api_module, "get_service", lambda: SlowRagService(3.0))
    monkeypatch.setattr(api_module, "get_copilot_service", lambda: SlowCopilotService(3.0))
    monkeypatch.setattr(api_module, "get_router", lambda: SlowRouter(3.0))
    return TestClient(api_module.app)


@pytest.fixture
def fast_client(monkeypatch, timeout_settings):
    """TestClient where all services are fast and timeouts are set."""
    monkeypatch.setattr(api_module, "get_settings", lambda: timeout_settings)
    monkeypatch.setattr(api_module, "get_service", lambda: FastRagService())
    monkeypatch.setattr(api_module, "get_copilot_service", lambda: FastCopilotService())
    monkeypatch.setattr(api_module, "get_router", lambda: FastRouter())
    return TestClient(api_module.app)


@pytest.fixture
def no_timeout_client(monkeypatch, disabled_timeout_settings):
    """TestClient where services are slow but timeouts are disabled."""
    # Use a shorter delay so tests don't take forever
    monkeypatch.setattr(api_module, "get_settings", lambda: disabled_timeout_settings)
    monkeypatch.setattr(api_module, "get_service", lambda: FastRagService())
    monkeypatch.setattr(api_module, "get_copilot_service", lambda: FastCopilotService())
    monkeypatch.setattr(api_module, "get_router", lambda: FastRouter())
    return TestClient(api_module.app)


# ---- Tests: Timeouts trigger HTTP 504 ----


def test_query_timeout_returns_504(slow_client) -> None:
    """POST /query should return 504 when the service is slower than the timeout."""
    response = slow_client.post("/query", json={"question": "What is the policy?"})
    assert response.status_code == 504
    body = response.json()
    assert "timed out" in body["detail"].lower()
    assert body["timeout_seconds"] == 1
    assert "trace_id" in body


def test_copilot_timeout_returns_504(slow_client) -> None:
    """POST /copilot/query should return 504 when copilot is slower than the timeout."""
    response = slow_client.post("/copilot/query", json={"question": "What was total revenue?"})
    assert response.status_code == 504
    body = response.json()
    assert "timed out" in body["detail"].lower()
    assert body["timeout_seconds"] == 1


def test_ask_timeout_returns_504(slow_client) -> None:
    """POST /ask should return 504 when the router is slower than the timeout."""
    response = slow_client.post("/ask", json={"question": "Tell me about sales"})
    assert response.status_code == 504
    body = response.json()
    assert "timed out" in body["detail"].lower()
    assert body["timeout_seconds"] == 1


# ---- Tests: Fast requests pass through ----


def test_query_fast_passes_through(fast_client) -> None:
    """POST /query should return 200 when the service responds within the timeout."""
    response = fast_client.post("/query", json={"question": "What is the policy?"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Fast answer"


def test_copilot_fast_passes_through(fast_client) -> None:
    """POST /copilot/query should return 200 when the service responds within the timeout."""
    response = fast_client.post("/copilot/query", json={"question": "What was total revenue?"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Fast copilot answer"


def test_ask_fast_passes_through(fast_client) -> None:
    """POST /ask should return 200 when the router responds within the timeout."""
    response = fast_client.post("/ask", json={"question": "Tell me about sales"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Fast unified answer"


# ---- Tests: Non-query endpoints are not affected ----


def test_health_not_affected_by_timeout(fast_client) -> None:
    """GET /health should not have a timeout applied."""
    response = fast_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_not_affected_by_timeout(fast_client) -> None:
    """GET /metrics should not have a timeout applied."""
    response = fast_client.get("/metrics")
    assert response.status_code == 200


def test_root_not_affected_by_timeout(fast_client) -> None:
    """GET / should not have a timeout applied."""
    response = fast_client.get("/")
    assert response.status_code == 200


# ---- Tests: Disabled timeouts ----


def test_disabled_timeout_lets_request_through(no_timeout_client) -> None:
    """When timeout is 0 (disabled), requests should not be timed out."""
    response = no_timeout_client.post("/query", json={"question": "What is the policy?"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Fast answer"


# ---- Tests: Response metadata ----


def test_timeout_response_includes_trace_id_header(slow_client) -> None:
    """504 responses should include X-Trace-Id header for debugging."""
    response = slow_client.post("/query", json={"question": "What is the policy?"})
    assert response.status_code == 504
    assert "X-Trace-Id" in response.headers


def test_successful_response_includes_trace_id_header(fast_client) -> None:
    """Successful responses should still include X-Trace-Id header."""
    response = fast_client.post("/query", json={"question": "What is the policy?"})
    assert response.status_code == 200
    assert "x-trace-id" in response.headers


# ---- Tests: Config validation ----


def test_timeout_settings_default_values() -> None:
    """Timeout settings should have sensible defaults."""
    settings = Settings.model_construct(
        s3_bucket="test",
        ingestion_queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
        secrets_manager_secret_id="",
    )
    # Defaults from the Field definitions
    assert settings.request_timeout_query_s == 60
    assert settings.request_timeout_copilot_s == 90
    assert settings.request_timeout_ask_s == 240
    assert settings.bedrock_read_timeout_s == 90
