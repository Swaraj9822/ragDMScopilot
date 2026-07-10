"""Rate limiting for the operator-only SQL Lab endpoints (backlog item).

Each ``POST /sql/run`` opens a fresh DB connection; combined with the guard's
function checks, an operator could otherwise hold many near-timeout connections.
A per-client sliding-window limiter caps the request rate.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.config import get_settings
from rag_system.sql_lab import router as sql_lab_router_mod
from rag_system.sql_lab.router import SqlRunResult, get_sql_lab_service


class _FakeService:
    def run(self, sql: str, identity: str) -> SqlRunResult:
        return SqlRunResult(
            columns=["n"], rows=[{"n": 1}], row_count=1, duration_ms=1, sql=sql, truncated=False
        )


def _client(rpm: int) -> TestClient:
    sql_lab_router_mod._sql_lab_limiters.clear()
    api_module.app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        sql_lab_rate_limit_per_minute=rpm,
        auth_enabled=True,
        operator_emails_set=frozenset(),
    )
    api_module.app.dependency_overrides[get_sql_lab_service] = lambda: _FakeService()
    return TestClient(api_module.app)


def teardown_function() -> None:
    api_module.app.dependency_overrides.pop(get_settings, None)
    api_module.app.dependency_overrides.pop(get_sql_lab_service, None)
    sql_lab_router_mod._sql_lab_limiters.clear()


def test_requests_within_limit_succeed() -> None:
    client = _client(rpm=5)
    for _ in range(5):
        assert client.post("/sql/run", json={"sql": "SELECT 1"}).status_code == 200


def test_exceeding_limit_returns_429_with_retry_after() -> None:
    client = _client(rpm=3)
    for _ in range(3):
        assert client.post("/sql/run", json={"sql": "SELECT 1"}).status_code == 200
    blocked = client.post("/sql/run", json={"sql": "SELECT 1"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_zero_disables_throttling() -> None:
    client = _client(rpm=0)
    for _ in range(50):
        assert client.post("/sql/run", json={"sql": "SELECT 1"}).status_code == 200
