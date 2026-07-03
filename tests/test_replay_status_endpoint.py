"""Unit tests for GET /replays/{id} endpoint (R8.10, task 14.9).

Covers:
- Success path: operator retrieves a replay run and gets its current state.
- Not-found: returns 404 when the replay run doesn't exist.
- Operator gating: non-operators receive 403.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.models import (
    ReplayRetrievalParams,
    ReplayRun,
    ReplayRunRequest,
    ReplayRunResult,
    ReplayRunState,
)
from rag_system.storage import replay_run_key

_OPERATOR = UserPublic(
    id="op-1",
    email="operator@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=True,
)

_NON_OPERATOR = UserPublic(
    id="user-1",
    email="user@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=False,
)


class _FakeArtifactStore:
    """Minimal fake store for GET /replays/{id} testing."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)


@pytest.fixture
def operator_client():
    """TestClient with operator gating satisfied."""
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    try:
        yield TestClient(api_module.app)
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


def _make_queued_run(run_id: str = "run-123") -> ReplayRun:
    return ReplayRun(
        replay_run_id=run_id,
        state=ReplayRunState.queued,
        request=ReplayRunRequest(
            question="What is the answer?",
            ai_configuration_version_id="default:v1",
            retrieval_params=ReplayRetrievalParams(max_passages=10, min_score=0.5),
            corpus_snapshot_id="snap-1",
        ),
    )


def _make_completed_run(run_id: str = "run-456") -> ReplayRun:
    return ReplayRun(
        replay_run_id=run_id,
        state=ReplayRunState.completed,
        request=ReplayRunRequest(
            question="How does X work?",
            ai_configuration_version_id="default:v2",
            retrieval_params=ReplayRetrievalParams(max_passages=20, min_score=0.3),
            corpus_snapshot_id="snap-2",
        ),
        result=ReplayRunResult(
            answer="X works by doing Y.",
            evidence=[],
            route="rag",
            retrieval_scores=[0.85, 0.72],
            latency_ms=1234.5,
            prompt_tokens=100,
            completion_tokens=50,
            cost=0.001,
        ),
    )


def test_get_replay_run_queued(operator_client, monkeypatch):
    """GET /replays/{id} returns a queued run with current state (R8.10)."""
    store = _FakeArtifactStore()
    run = _make_queued_run()
    store.objects[replay_run_key(run.replay_run_id)] = run.model_dump()
    monkeypatch.setattr(api_module, "_get_artifact_store", lambda: store)

    response = operator_client.get(f"/replays/{run.replay_run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["replay_run_id"] == "run-123"
    assert body["state"] == "queued"
    assert body["result"] is None
    assert body["failure_reason"] is None


def test_get_replay_run_completed(operator_client, monkeypatch):
    """GET /replays/{id} returns a completed run with result (R8.10)."""
    store = _FakeArtifactStore()
    run = _make_completed_run()
    store.objects[replay_run_key(run.replay_run_id)] = run.model_dump()
    monkeypatch.setattr(api_module, "_get_artifact_store", lambda: store)

    response = operator_client.get(f"/replays/{run.replay_run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["replay_run_id"] == "run-456"
    assert body["state"] == "completed"
    assert body["result"]["answer"] == "X works by doing Y."
    assert body["result"]["route"] == "rag"
    assert body["result"]["retrieval_scores"] == [0.85, 0.72]
    assert body["result"]["latency_ms"] == 1234.5
    assert body["result"]["prompt_tokens"] == 100
    assert body["result"]["completion_tokens"] == 50
    assert body["result"]["cost"] == 0.001


def test_get_replay_run_not_found(operator_client, monkeypatch):
    """GET /replays/{id} returns 404 when the run doesn't exist."""
    store = _FakeArtifactStore()
    monkeypatch.setattr(api_module, "_get_artifact_store", lambda: store)

    response = operator_client.get("/replays/nonexistent-run")

    assert response.status_code == 404
    assert response.json()["detail"] == "Replay run not found."


def test_get_replay_run_requires_operator(monkeypatch):
    """GET /replays/{id} rejects non-operators with 403."""
    from rag_system.auth import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
    try:
        client = TestClient(api_module.app)
        response = client.get("/replays/some-run-id")
        assert response.status_code == 403
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
