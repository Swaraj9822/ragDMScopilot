"""Tests for the evaluation-run endpoints (R7.7, task 12.1 wiring).

Regression: the frontend Evaluation page called ``GET /evaluation/runs`` and
``GET /evaluation/runs/{id}`` but no such routes existed (404). These are now
registered (operator-only) alongside a ``POST /evaluation/runs`` trigger.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.evaluation import EvaluationSetValidationError
from rag_system.models import (
    BenchmarkResult,
    DeterministicCheck,
    EvaluationRunDetail,
    EvaluationRunSummary,
)

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


def _detail(run_id: str = "run-1", ci_passed: bool = True) -> EvaluationRunDetail:
    return EvaluationRunDetail(
        run_id=run_id,
        created_at="2024-06-15T10:00:00+00:00",
        ci_passed=ci_passed,
        results=[
            BenchmarkResult(
                case_id="c1",
                deterministic_checks=[
                    DeterministicCheck(name="citation_presence", outcome="pass")
                ],
            )
        ],
    )


class _FakeService:
    def __init__(self) -> None:
        self.runs: dict[str, EvaluationRunDetail] = {}
        self.raise_validation = False

    def run_evaluation(self) -> EvaluationRunDetail:
        if self.raise_validation:
            raise EvaluationSetValidationError("no human-reviewed case")
        detail = _detail("run-new")
        self.runs[detail.run_id] = detail
        return detail

    def list_evaluation_runs(self) -> list[EvaluationRunSummary]:
        return [
            EvaluationRunSummary(
                run_id=d.run_id,
                created_at=d.created_at,
                ci_passed=d.ci_passed,
                result_count=len(d.results),
            )
            for d in self.runs.values()
        ]

    def get_evaluation_run(self, run_id: str) -> EvaluationRunDetail | None:
        return self.runs.get(run_id)


@pytest.fixture
def operator_client(monkeypatch):
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    service = _FakeService()
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    try:
        yield TestClient(api_module.app), service
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


def test_post_run_returns_detail(operator_client):
    client, service = operator_client
    resp = client.post("/evaluation/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-new"
    assert body["ci_passed"] is True
    assert len(body["results"]) == 1


def test_post_run_invalid_set_returns_400(operator_client):
    client, service = operator_client
    service.raise_validation = True
    resp = client.post("/evaluation/runs")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "evaluation_set_invalid"


def test_list_runs_returns_summaries(operator_client):
    client, service = operator_client
    service.runs["run-1"] = _detail("run-1")
    resp = client.get("/evaluation/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["run_id"] == "run-1"
    assert body[0]["result_count"] == 1


def test_get_run_detail_and_404(operator_client):
    client, service = operator_client
    service.runs["run-1"] = _detail("run-1")

    ok = client.get("/evaluation/runs/run-1")
    assert ok.status_code == 200
    assert ok.json()["run_id"] == "run-1"

    missing = client.get("/evaluation/runs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "evaluation_run_not_found"


def test_requires_operator(monkeypatch):
    from rag_system.auth.dependencies import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
    monkeypatch.setattr(api_module, "get_service", lambda: _FakeService())
    try:
        client = TestClient(api_module.app)
        for method, path in [
            ("get", "/evaluation/runs"),
            ("get", "/evaluation/runs/run-1"),
            ("post", "/evaluation/runs"),
        ]:
            resp = getattr(client, method)(path)
            assert resp.status_code == 403, f"{method} {path}"
            assert resp.json()["detail"] == "operator_required"
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
