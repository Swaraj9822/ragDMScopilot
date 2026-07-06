"""Unit tests for AI configuration API endpoints (R9.5, R9.6, task 15.5).

Covers:
- GET /ai-config/{id}/history returns an empty list when no versions exist (R9.6)
- Endpoints are operator-only (require_operator gating)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.ai_config import AIConfigurationStore
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.storage import PreconditionFailed

_OPERATOR = UserPublic(
    id="op-1",
    email="operator@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=True,
)


class _FakeStore:
    """In-memory stand-in for the GcsArtifactStore used by AIConfigurationStore."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        self._bucket = "test-bucket"

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    def get_json(self, key: str) -> object | None:
        entry = self.objects.get(key)
        return None if entry is None else entry[0]

    def get_json_with_etag(self, key: str) -> tuple[object | None, str | None]:
        entry = self.objects.get(key)
        if entry is None:
            return None, None
        return entry[0], entry[1]

    def put_json_conditional(
        self,
        key: str,
        payload: object,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        entry = self.objects.get(key)
        if if_none_match:
            if entry is not None:
                raise PreconditionFailed(key)
        elif if_match is not None:
            if entry is None or entry[1] != if_match:
                raise PreconditionFailed(key)
        self.objects[key] = (payload, self._next_etag())

    from rag_system.storage import GcsArtifactStore

    create_json = GcsArtifactStore.create_json
    update_json_cas = GcsArtifactStore.update_json_cas


@pytest.fixture
def operator_client(monkeypatch):
    """TestClient with operator gating satisfied and a fake artifact store."""
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    fake_store = _FakeStore()
    config_store = AIConfigurationStore(fake_store)
    monkeypatch.setattr(
        api_module, "_get_ai_config_store", lambda: config_store
    )
    try:
        yield TestClient(api_module.app), config_store
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


# ---------------------------------------------------------------------------
# Empty-history case (R9.6)
# ---------------------------------------------------------------------------


def test_get_history_returns_empty_list_when_no_versions_exist(operator_client):
    """GET /ai-config/{id}/history returns [] when no versions exist (R9.6)."""
    client, _ = operator_client
    response = client.get("/ai-config/nonexistent/history")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Basic endpoint smoke tests
# ---------------------------------------------------------------------------


def test_create_version_returns_201_with_valid_description(operator_client):
    """PUT /ai-config/{id} with valid description creates a version (R9.3)."""
    client, _ = operator_client
    response = client.put(
        "/ai-config/cfg-1",
        json={
            "prompt": "answer the question",
            "model": "gemini-3.5-flash",
            "router_threshold": 0.5,
            "change_description": "initial config",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["config_id"] == "cfg-1"
    assert body["change_description"] == "initial config"
    assert body["model"] == "gemini-3.5-flash"


def test_create_version_rejects_empty_description(operator_client):
    """PUT /ai-config/{id} with empty description returns 400 (R9.4)."""
    client, _ = operator_client
    response = client.put(
        "/ai-config/cfg-1",
        json={
            "prompt": "p",
            "model": "m",
            "router_threshold": 0.5,
            "change_description": "",
        },
    )
    # FastAPI validation rejects min_length=1 before hitting our handler
    assert response.status_code == 422


def test_history_returns_versions_after_creation(operator_client):
    """GET /ai-config/{id}/history returns versions reverse-chronologically (R9.5)."""
    client, config_store = operator_client
    # Create two versions directly via the store for deterministic ordering
    config_store.create_version(
        "cfg-1",
        prompt="p1",
        model="m1",
        router_threshold=0.5,
        change_description="first",
        version_id="v1",
        created_at="2024-01-01T00:00:00+00:00",
    )
    config_store.create_version(
        "cfg-1",
        prompt="p2",
        model="m2",
        router_threshold=0.6,
        change_description="second",
        version_id="v2",
        created_at="2024-02-01T00:00:00+00:00",
    )

    response = client.get("/ai-config/cfg-1/history")
    assert response.status_code == 200
    history = response.json()
    assert len(history) == 2
    assert history[0]["version_id"] == "v2"
    assert history[1]["version_id"] == "v1"


def test_rollback_unknown_version_returns_404(operator_client):
    """POST /ai-config/{id}/rollback with unknown version returns 404 (R9.9)."""
    client, _ = operator_client
    response = client.post(
        "/ai-config/cfg-1/rollback",
        json={"version_id": "nonexistent", "reason": "revert"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "configuration_version_not_found"


def test_rollback_valid_version_returns_activation_event(operator_client):
    """POST /ai-config/{id}/rollback with valid version returns activation event (R9.8)."""
    client, config_store = operator_client
    config_store.create_version(
        "cfg-1",
        prompt="p",
        model="m",
        router_threshold=0.5,
        change_description="initial",
        version_id="v1",
    )

    response = client.post(
        "/ai-config/cfg-1/rollback",
        json={"version_id": "v1", "reason": "activate initial"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_version_id"] == "v1"
    assert body["operator"] == "operator@example.com"
    assert body["reason"] == "activate initial"


# ---------------------------------------------------------------------------
# Approval endpoint (R8.3, R9.7 — task 15.10)
# ---------------------------------------------------------------------------


def test_approve_version_returns_approved_version(operator_client):
    """POST /ai-config/{id}/versions/{vid}/approve sets approval fields."""
    client, config_store = operator_client
    config_store.create_version(
        "cfg-1",
        prompt="answer carefully",
        model="gemini-3.5-flash",
        router_threshold=0.5,
        change_description="initial version",
        version_id="v1",
    )

    response = client.post("/ai-config/cfg-1/versions/v1/approve")
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is True
    assert body["approver"] == "operator@example.com"
    assert body["approved_at"] is not None
    # Governed settings unchanged
    assert body["prompt"] == "answer carefully"
    assert body["model"] == "gemini-3.5-flash"
    assert body["router_threshold"] == 0.5
    assert body["change_description"] == "initial version"


def test_approve_unknown_version_returns_404(operator_client):
    """POST /ai-config/{id}/versions/{vid}/approve → 404 for unknown version."""
    client, _ = operator_client
    response = client.post("/ai-config/cfg-1/versions/nonexistent/approve")
    assert response.status_code == 404
    assert response.json()["detail"] == "configuration_version_not_found"


def test_approve_endpoint_requires_operator():
    """POST /ai-config/{id}/versions/{vid}/approve → 403 for non-operator."""
    # Remove the operator override so require_operator actually rejects.
    api_module.app.dependency_overrides.pop(require_operator, None)
    non_operator = UserPublic(
        id="user-1",
        email="user@example.com",
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        is_operator=False,
    )
    from rag_system.auth import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: non_operator
    try:
        client = TestClient(api_module.app, raise_server_exceptions=False)
        response = client.post("/ai-config/cfg-1/versions/v1/approve")
        # Expect 403 with operator_required detail
        assert response.status_code == 403
        assert response.json()["detail"] == "operator_required"
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)


def test_approve_does_not_mutate_governed_settings_via_api(operator_client):
    """Approval via API preserves all governed fields unchanged."""
    client, config_store = operator_client
    config_store.create_version(
        "cfg-1",
        prompt="original prompt text",
        model="gemini-3.1-pro",
        router_threshold=0.75,
        change_description="tune thresholds",
        version_id="v1",
        output_schema={"type": "object"},
        retrieval_settings={"top_k": 10},
    )

    response = client.post("/ai-config/cfg-1/versions/v1/approve")
    assert response.status_code == 200
    body = response.json()

    # Governed settings must be identical to creation values
    assert body["prompt"] == "original prompt text"
    assert body["model"] == "gemini-3.1-pro"
    assert body["router_threshold"] == 0.75
    assert body["output_schema"] == {"type": "object"}
    assert body["retrieval_settings"] == {"top_k": 10}
    assert body["change_description"] == "tune thresholds"

    # Approval metadata set
    assert body["approved"] is True
    assert body["approver"] == "operator@example.com"
