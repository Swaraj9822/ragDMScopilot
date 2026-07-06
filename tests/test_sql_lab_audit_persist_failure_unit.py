"""Unit tests for the audit-persist failure path (task 11.4).

Requirement 8.6:

    IF persisting an audit record fails, THEN THE SQL_Lab_Backend SHALL return
    an error response indicating the request could not be recorded and SHALL NOT
    return query result rows to the caller.

Auditing is *mandatory*: on a successful execution the
:class:`~rag_system.sql_lab.service.SqlLabService` persists the ``success``
audit record **before** returning the shaped Result_Set. If that persistence
fails, the store raises :class:`~rag_system.sql_lab.errors.SqlLabAuditError`;
the service lets it propagate instead of returning the rows, so the caller never
receives a Result_Set for an unrecorded request (R8.6).

These tests exercise that guarantee at two layers:

1. **Service level (primary).** A fake executor returns a successful
   :class:`~rag_system.sql_lab.executor.ExecutionResult` and a fake audit store
   whose ``persist`` raises :class:`SqlLabAuditError`. ``service.run(...)`` must
   raise ``SqlLabAuditError`` and must not return a :class:`SqlRunResult` — the
   rows are withheld.
2. **Route level (defense-in-depth).** Through the FastAPI ``TestClient`` with
   dependency overrides, a persist failure maps to ``500`` and the response body
   carries no result rows.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.config import Settings
from rag_system.sql_lab.errors import SqlLabAuditError
from rag_system.sql_lab.executor import ExecutionResult
from rag_system.sql_lab.router import get_sql_lab_service
from rag_system.sql_lab.service import SqlLabService, SqlRunResult

# Required Settings supplied by alias so the model builds in isolation
# (mirrors the convention in the sibling SQL Lab tests).
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(**overrides: object) -> Settings:
    """Construct ``Settings`` with the required aliases plus any overrides."""
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


class _AllowAllGuard:
    """Guard stub that approves every statement (returns it unchanged).

    The audit-persist path is only reached for a guard-*approved* statement, so
    this stub lets the test focus on the success → persist → withhold sequence
    without depending on real guard parsing.
    """

    def validate(self, sql: str) -> str:
        return sql


class _SuccessExecutor:
    """Executor stub that returns a canned successful :class:`ExecutionResult`.

    Records the SQL it was called with so a test can confirm the executor did
    run (the failure is in persistence, not execution).
    """

    def __init__(self) -> None:
        self.called_with: str | None = None

    def execute(self, sql: str) -> ExecutionResult:
        self.called_with = sql
        return ExecutionResult(
            columns=["id"],
            rows=[{"id": 1}, {"id": 2}],
            row_count=2,
            duration_ms=5,
            truncated=False,
        )


class _FailingAuditStore:
    """Audit store stub whose ``persist`` always raises ``SqlLabAuditError``.

    Records every record it was asked to persist so the test can assert the
    service attempted to record the (success) outcome before the failure.
    """

    def __init__(self) -> None:
        self.attempts: list[object] = []

    def persist(self, record: object) -> None:
        self.attempts.append(record)
        raise SqlLabAuditError("Failed to persist the SQL Lab audit record.")


def _service(
    executor: _SuccessExecutor, audit_store: _FailingAuditStore
) -> SqlLabService:
    """Wire a service with an allow-all guard, the given executor, and store."""
    return SqlLabService(
        _build_settings(),
        guard=_AllowAllGuard(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        audit_store=audit_store,  # type: ignore[arg-type]
    )


# --- Service level (R8.6) ----------------------------------------------------


def test_persist_failure_raises_and_withholds_result():
    """A success-record persist failure raises SqlLabAuditError, not a Result_Set."""
    executor = _SuccessExecutor()
    store = _FailingAuditStore()
    service = _service(executor, store)

    result = None
    with pytest.raises(SqlLabAuditError):
        result = service.run("SELECT id FROM widgets", "operator@example.com")

    # No Result_Set is returned to the caller — the rows are withheld (R8.6).
    assert result is None
    # The statement did execute; the failure is purely in recording the outcome.
    assert executor.called_with == "SELECT id FROM widgets"
    # The service attempted to persist exactly the (success) audit record.
    assert len(store.attempts) == 1


def test_persist_failure_returns_no_rows_object():
    """The withheld outcome is an exception, never a partially-populated result."""
    service = _service(_SuccessExecutor(), _FailingAuditStore())

    with pytest.raises(SqlLabAuditError):
        run_result = service.run("SELECT 1", "operator@example.com")
        # Unreachable: if run() ever returned, it must not be a SqlRunResult
        # carrying rows.
        assert not isinstance(run_result, SqlRunResult)


# --- Route level (R8.6, defense-in-depth) ------------------------------------

_OPERATOR = UserPublic(
    id="op-1",
    email="operator@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=True,
)


class _FakeRunService:
    """Route-level service stub whose ``run`` raises ``SqlLabAuditError``."""

    def run(self, sql: str, user_identity: str) -> SqlRunResult:  # noqa: ARG002
        raise SqlLabAuditError(
            "Failed to persist the SQL Lab audit record."
        )


def test_route_persist_failure_maps_to_500_with_no_rows():
    """A persist failure maps to 500 and the body carries no result rows (R8.6)."""
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    api_module.app.dependency_overrides[get_sql_lab_service] = lambda: _FakeRunService()
    try:
        client = TestClient(api_module.app)
        response = client.post("/sql/run", json={"sql": "SELECT 1"})
        assert response.status_code == 500
        body = response.json()
        # No result rows are present in the error body.
        assert "rows" not in body
        assert "could not be recorded" in body["detail"]
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)
        api_module.app.dependency_overrides.pop(get_sql_lab_service, None)
