"""Unit tests for the ``POST /sql/analyze`` route (task 12.7).

Covers, using the FastAPI ``TestClient`` and dependency overrides:

* Auth gating — a request without a JWT gets ``401`` (R9.1) and an authenticated
  non-operator gets ``403`` (R9.1), before any data reaches the language model.
* Model selection (R9.8/R9.9) — posting without a ``mode`` drives the analyzer
  in ``"default"`` mode (Gemini Flash) and ``mode="deep"`` drives it in
  ``"deep"`` mode (Gemini Pro). The route threads ``mode`` straight through to
  :meth:`ChartSpecAnalyzer.analyze`, so asserting the mode the analyzer was
  called with proves the selection reaches the model layer.
* LLM unavailable/slow (R9.10) — when the analyzer raises
  :class:`~rag_system.sql_lab.errors.SqlLabAnalysisError` (model unavailable or
  over the 60s budget) the route maps it to ``503``.
* Invalid spec (R9.6) — when the analyzer raises
  :class:`~rag_system.sql_lab.chart_spec.ChartSpecValidationError` the route
  maps it to ``400``.

The analyzer is replaced with a fake via the ``get_chart_spec_analyzer``
dependency override so each path is exercised in isolation without the
``google-genai`` dependency or a live model. Operator gating is satisfied by
overriding ``require_operator`` (matching the pattern used by the other
operator-only endpoint tests).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.dependencies import get_current_user
from rag_system.auth.models import UserPublic
from rag_system.sql_lab.chart_spec import (
    ChartDef,
    ChartSpec,
    ChartSpecValidationError,
    KpiSpec,
    SeriesSpec,
)
from rag_system.sql_lab.errors import SqlLabAnalysisError
from rag_system.sql_lab.gemini_client import AnalysisMode
from rag_system.sql_lab.router import get_chart_spec_analyzer

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

#: A minimal but fully valid Chart_Spec the fake analyzer returns for the
#: success / model-selection cases (1 KPI, 1 chart, a bounded insight).
_VALID_SPEC = ChartSpec(
    kpis=[KpiSpec(label="Total rows", op="count", column="id")],
    charts=[
        ChartDef(
            type="bar",
            title="Amount by category",
            xColumn="category",
            series=[SeriesSpec(column="amount", op="sum")],
        )
    ],
    insight="Category A accounts for most of the total amount.",
)

#: A representative request body (the camelCase Result_Set shape the frontend
#: posts). The concrete values are irrelevant because the analyzer is faked.
_REQUEST_BODY: dict[str, Any] = {
    "columns": ["id", "category", "amount"],
    "rows": [
        {"id": 1, "category": "A", "amount": 10},
        {"id": 2, "category": "B", "amount": 5},
    ],
    "rowCount": 2,
}


class _FakeAnalyzer:
    """A stand-in :class:`ChartSpecAnalyzer` for the route.

    ``analyze`` records the ``mode`` it was called with (so tests can assert the
    default-vs-deep selection reaches the analyzer) and either raises the
    configured exception or returns the canned :data:`_VALID_SPEC`.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.called_with_mode: AnalysisMode | None = None
        self.called_with_result: Any = None

    def analyze(self, result: Any, mode: AnalysisMode = "default") -> ChartSpec:
        self.called_with_result = result
        self.called_with_mode = mode
        if self._raises is not None:
            raise self._raises
        return _VALID_SPEC


def _use_analyzer(analyzer: _FakeAnalyzer) -> None:
    api_module.app.dependency_overrides[get_chart_spec_analyzer] = lambda: analyzer


@pytest.fixture
def operator_client():
    """A ``TestClient`` with operator gating satisfied.

    Yields a helper that installs a :class:`_FakeAnalyzer` and returns the
    client, so each test can wire the analyzer behaviour it needs.
    """
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    try:
        client = TestClient(api_module.app)

        def _run(analyzer: _FakeAnalyzer):
            _use_analyzer(analyzer)
            return client

        yield _run
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)
        api_module.app.dependency_overrides.pop(get_chart_spec_analyzer, None)


# --- Auth gating (R9.1) ------------------------------------------------------


def test_analyze_without_jwt_returns_401():
    """A request without a valid JWT is rejected with 401 (R9.1)."""
    # Drop the suite-wide anonymous auth override so the real get_current_user
    # runs with no bearer credential and raises 401.
    api_module.app.dependency_overrides.pop(get_current_user, None)
    try:
        client = TestClient(api_module.app)
        response = client.post("/sql/analyze", json=_REQUEST_BODY)
        assert response.status_code == 401
    finally:
        # conftest's autouse fixture re-installs the override on teardown.
        pass


def test_analyze_non_operator_returns_403():
    """An authenticated non-operator is rejected with 403 (R9.1)."""
    api_module.app.dependency_overrides.pop(require_operator, None)
    api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
    try:
        client = TestClient(api_module.app)
        response = client.post("/sql/analyze", json=_REQUEST_BODY)
        assert response.status_code == 403
        assert "operator_required" in response.json().get("detail", "")
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)


# --- Model selection: default vs deep (R9.8, R9.9) --------------------------


def test_analyze_without_mode_selects_default(operator_client):
    """Posting without a mode drives the analyzer in "default" mode (R9.8)."""
    analyzer = _FakeAnalyzer()
    client = operator_client(analyzer)
    response = client.post("/sql/analyze", json=_REQUEST_BODY)
    assert response.status_code == 200
    # The route threaded the default mode through to the analyzer (→ Flash).
    assert analyzer.called_with_mode == "default"
    body = response.json()
    assert body["kpis"][0]["label"] == "Total rows"
    assert body["charts"][0]["xColumn"] == "category"


def test_analyze_with_deep_mode_selects_deep(operator_client):
    """Posting mode="deep" drives the analyzer in "deep" mode (R9.9)."""
    analyzer = _FakeAnalyzer()
    client = operator_client(analyzer)
    response = client.post("/sql/analyze", json={**_REQUEST_BODY, "mode": "deep"})
    assert response.status_code == 200
    # The route threaded the deep mode through to the analyzer (→ Pro).
    assert analyzer.called_with_mode == "deep"


def test_analyze_forwards_result_set_to_analyzer(operator_client):
    """The route hands the source Result_Set columns/rows to the analyzer (R9.2)."""
    analyzer = _FakeAnalyzer()
    client = operator_client(analyzer)
    response = client.post("/sql/analyze", json=_REQUEST_BODY)
    assert response.status_code == 200
    assert analyzer.called_with_result["columns"] == ["id", "category", "amount"]
    assert analyzer.called_with_result["rowCount"] == 2


# --- LLM unavailable / slow (R9.10) -----------------------------------------


def test_analyze_llm_unavailable_maps_to_503(operator_client):
    """An unavailable/slow model (SqlLabAnalysisError) maps to 503 (R9.10)."""
    analyzer = _FakeAnalyzer(
        raises=SqlLabAnalysisError(
            "Analysis could not be completed: the analysis model is unavailable."
        )
    )
    client = operator_client(analyzer)
    response = client.post("/sql/analyze", json=_REQUEST_BODY)
    assert response.status_code == 503
    assert "could not be completed" in response.json()["detail"]


# --- Invalid spec (R9.6) -----------------------------------------------------


def test_analyze_invalid_spec_maps_to_400(operator_client):
    """A Chart_Spec validation failure maps to 400 (R9.6)."""
    analyzer = _FakeAnalyzer(
        raises=ChartSpecValidationError("Chart_Spec failed schema validation")
    )
    client = operator_client(analyzer)
    response = client.post("/sql/analyze", json=_REQUEST_BODY)
    assert response.status_code == 400
    assert "schema validation" in response.json()["detail"]
