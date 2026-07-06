"""Unit tests for the ``POST /sql/run`` route (task 5.2).

Covers, using the FastAPI ``TestClient`` and dependency overrides:

* Auth gating — a request without a JWT gets ``401`` (R4.2) and an authenticated
  non-operator gets ``403`` (R4.3), before any handler logic runs.
* SQL length boundary — a 10000-character body is accepted (reaches the service)
  and a 10001-character body is rejected with ``400`` (R4.1).
* Empty/whitespace bodies are rejected with ``400`` (R4.12).
* Error mapping (R4.9, R4.12, R4.13): the guard rejection and the
  config/connection/execution errors map to ``400`` (execution carries the db
  message), and the timeout error maps to ``504``.

The service is replaced with a fake via the ``get_sql_lab_service`` dependency
override so each error-status mapping can be exercised in isolation without a
real database. Operator gating is satisfied by overriding ``require_operator``
(matching the pattern used by the other operator-only endpoint tests).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.dependencies import get_current_user
from rag_system.auth.models import UserPublic
from rag_system.sql_lab.errors import (
    SqlLabConfigError,
    SqlLabConnectionError,
    SqlLabExecutionError,
    SqlLabTimeoutError,
)
from rag_system.sql_lab.guard import SqlLabValidationError
from rag_system.sql_lab.router import get_sql_lab_service
from rag_system.sql_lab.service import SqlRunResult

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


class _FakeService:
    """A stand-in :class:`SqlLabService` for the route.

    ``run`` either raises the configured exception (to exercise a mapping) or
    returns a canned :class:`SqlRunResult`. It records the SQL it was called
    with so tests can assert the handler actually reached the service (e.g. the
    accepted length boundary).
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.called_with: str | None = None
        self.called_identity: str | None = None

    def run(self, sql: str, user_identity: str) -> SqlRunResult:
        self.called_with = sql
        self.called_identity = user_identity
        if self._raises is not None:
            raise self._raises
        return SqlRunResult(
            columns=["id"],
            rows=[{"id": 1}],
            row_count=1,
            duration_ms=3,
            sql=sql,
            truncated=False,
        )


def _use_service(service: _FakeService) -> None:
    api_module.app.dependency_overrides[get_sql_lab_service] = lambda: service


@pytest.fixture
def operator_client():
    """A ``TestClient`` with operator gating satisfied.

    Yields a helper that installs a :class:`_FakeService` and returns the
    client, so each test can wire the service behaviour it needs.
    """
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    try:
        client = TestClient(api_module.app)

        def _run(service: _FakeService):
            _use_service(service)
            return client

        yield _run
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)
        api_module.app.dependency_overrides.pop(get_sql_lab_service, None)


# --- Auth gating (R4.2, R4.3) ------------------------------------------------


def test_run_without_jwt_returns_401():
    """A request without a valid JWT is rejected with 401 (R4.2)."""
    # Drop the suite-wide anonymous auth override so the real get_current_user
    # runs with no bearer credential and raises 401.
    api_module.app.dependency_overrides.pop(get_current_user, None)
    try:
        client = TestClient(api_module.app)
        response = client.post("/sql/run", json={"sql": "SELECT 1"})
        assert response.status_code == 401
    finally:
        # conftest's autouse fixture re-installs the override on teardown.
        pass


def test_run_non_operator_returns_403():
    """An authenticated non-operator is rejected with 403 (R4.3)."""
    api_module.app.dependency_overrides.pop(require_operator, None)
    api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
    try:
        client = TestClient(api_module.app)
        response = client.post("/sql/run", json={"sql": "SELECT 1"})
        assert response.status_code == 403
        assert "operator_required" in response.json().get("detail", "")
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)


# --- SQL length boundary (R4.1) ---------------------------------------------


def test_run_accepts_sql_at_max_length(operator_client):
    """A 10000-character SQL string is accepted and reaches the service (R4.1)."""
    service = _FakeService()
    client = operator_client(service)
    sql = "s" * 10_000
    response = client.post("/sql/run", json={"sql": sql})
    assert response.status_code == 200
    assert service.called_with == sql
    body = response.json()
    assert body["columns"] == ["id"]
    assert body["rowCount"] == 1


def test_run_rejects_sql_over_max_length(operator_client):
    """A 10001-character SQL string is rejected with 400 (R4.1)."""
    service = _FakeService()
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": "s" * 10_001})
    assert response.status_code == 400
    assert "10000" in response.json()["detail"]
    # The over-length body never reaches the service.
    assert service.called_with is None


# --- Empty / whitespace validation (R4.12) ----------------------------------


@pytest.mark.parametrize("sql", ["", "   ", "\t\n  "])
def test_run_rejects_empty_or_whitespace(operator_client, sql):
    """An empty or whitespace-only body is rejected with 400 (R4.12)."""
    service = _FakeService()
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": sql})
    assert response.status_code == 400
    assert service.called_with is None


# --- Error-status mapping (R4.9, R4.12, R4.13) ------------------------------


def test_guard_rejection_maps_to_400(operator_client):
    """A guard rejection (SqlLabValidationError) maps to 400 (R4.12)."""
    service = _FakeService(raises=SqlLabValidationError("multiple statements"))
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": "SELECT 1; SELECT 2"})
    assert response.status_code == 400
    assert "multiple statements" in response.json()["detail"]


def test_config_error_maps_to_400(operator_client):
    """A missing-credentials config error maps to 400 with a keyed message."""
    service = _FakeService(
        raises=SqlLabConfigError("Missing SQL Lab configuration: SQL_VIEWER_DB_USER")
    )
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": "SELECT 1"})
    assert response.status_code == 400
    assert "SQL_VIEWER_DB_USER" in response.json()["detail"]


def test_connection_error_maps_to_400(operator_client):
    """A viewer-connection failure maps to 400 (R4.6-style keyed message)."""
    service = _FakeService(
        raises=SqlLabConnectionError("viewer database connection failed")
    )
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": "SELECT 1"})
    assert response.status_code == 400
    assert "connection failed" in response.json()["detail"]


def test_timeout_error_maps_to_504(operator_client):
    """A statement timeout maps to 504 (R4.9)."""
    service = _FakeService(
        raises=SqlLabTimeoutError("Statement_Timeout of 10000 ms exceeded")
    )
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": "SELECT pg_sleep(60)"})
    assert response.status_code == 504
    assert "exceeded" in response.json()["detail"]


def test_execution_error_maps_to_400_with_db_message(operator_client):
    """A generic db error maps to 400 and carries the db message (R4.13)."""
    service = _FakeService(
        raises=SqlLabExecutionError('relation "nope" does not exist')
    )
    client = operator_client(service)
    response = client.post("/sql/run", json={"sql": "SELECT * FROM nope"})
    assert response.status_code == 400
    assert 'relation "nope" does not exist' in response.json()["detail"]
