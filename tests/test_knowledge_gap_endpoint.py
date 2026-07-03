"""Unit tests for POST /knowledge-gap-map endpoint (R11.5, task 18.4).

Covers:
- Generation-failure error returns 500 with ``knowledge_gap_generation_failed``
- Operator-only gating (returns 403 for non-operators)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.knowledge_gap import KnowledgeGapGenerationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def operator_client(monkeypatch):
    """TestClient with operator gating satisfied and mocked service/store.

    The FakeStore mimics the GcsArtifactStore listing interface (returns empty
    trace/feedback key lists), so the endpoint reaches the
    generate_knowledge_gap_map call path without a real GCS client.
    """
    api_module.app.dependency_overrides[require_operator] = lambda: None

    class FakeStore:
        """Minimal GCS-shaped store that returns no traces or feedback."""

        def list_query_trace_keys(self) -> list[str]:
            return []

        def list_feedback_record_keys(self) -> list[str]:
            return []

        def get_json(self, key: str):
            return None

    class FakeService:
        artifact_store = FakeStore()

    monkeypatch.setattr(api_module, "get_service", lambda: FakeService())

    try:
        yield TestClient(api_module.app)
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_generation_failure_returns_knowledge_gap_generation_failed(
    operator_client,
) -> None:
    """When generate_knowledge_gap_map raises KnowledgeGapGenerationError,
    the endpoint returns 500 with detail ``knowledge_gap_generation_failed`` (R11.5).
    """
    client = operator_client

    # Patch generate_knowledge_gap_map where it's imported inside the endpoint
    # (lazy import from rag_system.knowledge_gap), and also patch the helper
    # functions that construct infrastructure dependencies (embedder, LLM).
    with patch(
        "rag_system.knowledge_gap.generate_knowledge_gap_map",
        side_effect=KnowledgeGapGenerationError("embedding service unavailable"),
    ), patch(
        "rag_system.api._get_embed_question",
        return_value=lambda q: [0.0] * 256,
    ), patch(
        "rag_system.api._get_label_cluster",
        return_value=lambda qs: "topic",
    ):
        resp = client.post("/knowledge-gap-map")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "knowledge_gap_generation_failed"


def test_operator_gating_rejects_non_operator(monkeypatch) -> None:
    """Non-operators get 403 from the require_operator dependency."""
    from rag_system.auth import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1", email="user@test.com", is_operator=False
    )
    try:
        client = TestClient(api_module.app)
        resp = client.post("/knowledge-gap-map")
        # The real require_operator rejects non-operators with 403.
        assert resp.status_code in (401, 403)
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
