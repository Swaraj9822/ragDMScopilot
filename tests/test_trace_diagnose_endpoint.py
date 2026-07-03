"""Unit tests for POST /traces/{id}/diagnose endpoint (R10.1, R10.6, task 17.4).

Covers:
- Success path: operator calls diagnose on a recorded trace and receives
  a TraceDiagnosis with cause and recommendations.
- Trace-not-found: returns 404 with ``trace_not_found`` detail.
- Operator gating: non-operators receive 403.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.models import Recommendation, TraceDiagnosis
from rag_system.trace_investigator import TraceNotFoundError

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


class _FakeInvestigator:
    """A stub TraceInvestigator that returns a canned diagnosis or raises."""

    def __init__(self, diagnosis: TraceDiagnosis | None = None) -> None:
        self._diagnosis = diagnosis

    def diagnose(self, trace_id: str) -> TraceDiagnosis:
        if self._diagnosis is None:
            raise TraceNotFoundError(trace_id)
        return self._diagnosis


@pytest.fixture
def operator_client():
    """TestClient with operator gating satisfied."""
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    try:
        yield TestClient(api_module.app)
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


def test_diagnose_trace_success(operator_client, monkeypatch):
    """POST /traces/{id}/diagnose returns TraceDiagnosis on success (R10.1, R10.6)."""
    expected = TraceDiagnosis(
        trace_id="trace-abc",
        cause_description="Low retrieval scores indicate poor document coverage.",
        analyzed_elements=["retrieval_scores"],
        recommendations=[
            Recommendation(
                description="Add more documents covering the query topic.",
                target="corpus",
            ),
        ],
    )
    fake_investigator = _FakeInvestigator(diagnosis=expected)
    monkeypatch.setattr(
        api_module, "_get_trace_investigator", lambda: fake_investigator
    )

    response = operator_client.post("/traces/trace-abc/diagnose")
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == "trace-abc"
    assert body["cause_description"] == expected.cause_description
    assert body["analyzed_elements"] == ["retrieval_scores"]
    assert len(body["recommendations"]) == 1
    assert body["recommendations"][0]["target"] == "corpus"


def test_diagnose_trace_not_found(operator_client, monkeypatch):
    """POST /traces/{id}/diagnose returns 404 for an unrecorded trace (R10.2)."""
    fake_investigator = _FakeInvestigator(diagnosis=None)
    monkeypatch.setattr(
        api_module, "_get_trace_investigator", lambda: fake_investigator
    )

    response = operator_client.post("/traces/nonexistent-trace/diagnose")
    assert response.status_code == 404
    assert response.json()["detail"] == "trace_not_found"


def test_diagnose_trace_requires_operator(monkeypatch):
    """POST /traces/{id}/diagnose returns 403 for a non-operator."""
    from rag_system.auth import get_current_user

    # Remove any operator override and inject a non-operator user.
    api_module.app.dependency_overrides.pop(require_operator, None)
    api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
    try:
        client = TestClient(api_module.app)
        response = client.post("/traces/some-trace/diagnose")
        assert response.status_code == 403
        assert "operator_required" in response.json().get("detail", "")
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
