"""Unit tests for POST /replays/{id}/cancel endpoint (R8.9, task 14.16).

Covers:
- Cancelling a queued run transitions it to cancelled with cancel_requested=True.
- Cancelling a running run transitions it to cancelled with cancel_requested=True.
- Cancelling a terminal run (completed/failed/cancelled) is a no-op.
- Not-found: returns 404 for a nonexistent run.
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


class _FakeStore:
    """Minimal fake store supporting get_json and update_json_cas for cancel tests."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def update_json_cas(self, key: str, mutate_fn) -> str:
        current = self.objects.get(key)
        new_val = mutate_fn(current)
        self.objects[key] = new_val
        return "etag-new"


def _make_request() -> ReplayRunRequest:
    return ReplayRunRequest(
        question="What is the answer?",
        ai_configuration_version_id="default:v1",
        retrieval_params=ReplayRetrievalParams(max_passages=10, min_score=0.5),
        corpus_snapshot_id="snap-1",
    )


def _make_run(state: ReplayRunState, run_id: str = "run-cancel-1") -> ReplayRun:
    result = None
    failure_reason = None
    if state == ReplayRunState.completed:
        result = ReplayRunResult(
            answer="The answer is 42.",
            evidence=[],
            route="rag",
            retrieval_scores=[0.9],
            latency_ms=500.0,
            prompt_tokens=80,
            completion_tokens=30,
            cost=0.0005,
        )
    elif state == ReplayRunState.failed:
        failure_reason = "Timeout exceeded"
    return ReplayRun(
        replay_run_id=run_id,
        state=state,
        request=_make_request(),
        result=result,
        failure_reason=failure_reason,
    )


@pytest.fixture
def operator_client():
    """TestClient with operator gating satisfied."""
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    try:
        yield TestClient(api_module.app)
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


def _setup_store_with_run(monkeypatch, run: ReplayRun) -> _FakeStore:
    """Set up a fake store with a run and patch api_module to use it."""
    store = _FakeStore()
    store.objects[replay_run_key(run.replay_run_id)] = run.model_dump()

    from rag_system.replay import ReplayService

    service = ReplayService(store, config_store=store)
    monkeypatch.setattr(api_module, "_get_replay_service", lambda: service)
    return store


def test_cancel_queued_run(operator_client, monkeypatch):
    """POST /replays/{id}/cancel transitions a queued run to cancelled (R8.9)."""
    run = _make_run(ReplayRunState.queued)
    store = _setup_store_with_run(monkeypatch, run)

    response = operator_client.post(f"/replays/{run.replay_run_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "cancelled"
    assert body["cancel_requested"] is True
    assert body["result"] is None
    assert body["failure_reason"] is None

    # Verify store was updated
    persisted = store.objects[replay_run_key(run.replay_run_id)]
    assert persisted["state"] == "cancelled"
    assert persisted["cancel_requested"] is True


def test_cancel_running_run(operator_client, monkeypatch):
    """POST /replays/{id}/cancel transitions a running run to cancelled (R8.9)."""
    run = _make_run(ReplayRunState.running)
    _setup_store_with_run(monkeypatch, run)

    response = operator_client.post(f"/replays/{run.replay_run_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "cancelled"
    assert body["cancel_requested"] is True
    assert body["result"] is None
    assert body["failure_reason"] is None


def test_cancel_completed_run_is_noop(operator_client, monkeypatch):
    """POST /replays/{id}/cancel on a completed run is a no-op (R8.9)."""
    run = _make_run(ReplayRunState.completed)
    _setup_store_with_run(monkeypatch, run)

    response = operator_client.post(f"/replays/{run.replay_run_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "completed"
    # Result remains intact
    assert body["result"]["answer"] == "The answer is 42."
    # cancel_requested is NOT set — the run was already terminal
    assert body["cancel_requested"] is False


def test_cancel_failed_run_is_noop(operator_client, monkeypatch):
    """POST /replays/{id}/cancel on a failed run is a no-op (R8.9)."""
    run = _make_run(ReplayRunState.failed)
    _setup_store_with_run(monkeypatch, run)

    response = operator_client.post(f"/replays/{run.replay_run_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "failed"
    assert body["failure_reason"] == "Timeout exceeded"


def test_cancel_already_cancelled_run_is_noop(operator_client, monkeypatch):
    """POST /replays/{id}/cancel on an already cancelled run is a no-op (R8.9)."""
    run = _make_run(ReplayRunState.cancelled)
    _setup_store_with_run(monkeypatch, run)

    response = operator_client.post(f"/replays/{run.replay_run_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "cancelled"


def test_cancel_nonexistent_run_returns_404(operator_client, monkeypatch):
    """POST /replays/{id}/cancel returns 404 when run doesn't exist."""
    store = _FakeStore()

    from rag_system.replay import ReplayService

    service = ReplayService(store, config_store=store)
    monkeypatch.setattr(api_module, "_get_replay_service", lambda: service)

    response = operator_client.post("/replays/nonexistent-run/cancel")

    assert response.status_code == 404
    assert response.json()["detail"] == "Replay run not found."


def test_cancel_requires_operator(monkeypatch):
    """POST /replays/{id}/cancel rejects non-operators with 403."""
    from rag_system.auth import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
    try:
        client = TestClient(api_module.app)
        response = client.post("/replays/some-run-id/cancel")
        assert response.status_code == 403
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
