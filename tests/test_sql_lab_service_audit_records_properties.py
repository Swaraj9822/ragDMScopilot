"""Property test: each request produces exactly one well-formed audit record (task 11.3).

# Feature: sql-lab, Property 12: Each request produces exactly one well-formed audit record

Property statement:

    *For any* request outcome (success / post-guard error / guard rejection),
    exactly one well-formed audit record is persisted carrying the user
    identity, submitted SQL (truncated to <= 10000 chars), a UTC timestamp, and
    the correct outcome — with duration and row_count present for success, and a
    rejection carrying neither.

:class:`~rag_system.sql_lab.service.SqlLabService` persists exactly one
:class:`~rag_system.sql_lab.audit_store.SqlLabAuditRecord` per request outcome
through the injected ``audit_store``:

* **rejected** — the guard raises
  :class:`~rag_system.sql_lab.guard.SqlLabValidationError`; a single ``rejected``
  record is persisted before the error re-raises, carrying neither a duration
  nor a row count (R8.3).
* **error** — a guard-approved statement fails in the executor (any
  :class:`~rag_system.sql_lab.errors.SqlLabError`); a single ``error`` record is
  persisted before the error re-raises (R8.2).
* **success** — the statement executes; a single ``success`` record carrying the
  measured duration and returned row count is persisted before the Result_Set is
  returned (R8.1).

This test wires **fakes** for the guard and executor (so the outcome is driven
deterministically without a database) and a recording in-memory audit store (so
persistence is captured without a live connection). For every generated
(user identity, SQL, outcome) triple it asserts exactly one record is persisted,
that the record carries the user identity (R8.4), the SQL truncated to
<= 10000 characters (R8.5), a UTC ``created_at`` timestamp, and the correct
outcome with the duration/row-count presence rules above.

**Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**
"""

from __future__ import annotations

import string
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.sql_lab.audit_store import AUDIT_MAX_SQL_LENGTH, SqlLabAuditRecord
from rag_system.sql_lab.errors import (
    SqlLabConfigError,
    SqlLabConnectionError,
    SqlLabError,
    SqlLabExecutionError,
    SqlLabTimeoutError,
)
from rag_system.sql_lab.executor import ExecutionResult
from rag_system.sql_lab.guard import SqlLabValidationError
from rag_system.sql_lab.service import SqlLabService


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


# ---------------------------------------------------------------------------
# Fakes: a guard and executor whose behaviour is driven per generated scenario,
# and a recording audit store that captures persisted records without a DB.
# ---------------------------------------------------------------------------


class _FakeGuard:
    """Guard fake that either allows (returns the SQL) or rejects.

    When ``reject`` is set the guard raises :class:`SqlLabValidationError`,
    driving the ``rejected`` outcome; otherwise it echoes the SQL back like the
    real guard's normalized return value.
    """

    def __init__(self, *, reject: bool) -> None:
        self._reject = reject

    def validate(self, sql: str) -> str:
        if self._reject:
            raise SqlLabValidationError("Rejected: fake guard rejection.")
        return sql


class _FakeExecutor:
    """Executor fake that either returns a result or raises a SqlLabError.

    ``result`` drives the ``success`` outcome; ``error`` drives the post-guard
    ``error`` outcome. The fake is never constructed for the ``rejected``
    outcome because the guard short-circuits before the executor is reached.
    """

    def __init__(
        self,
        *,
        result: ExecutionResult | None = None,
        error: SqlLabError | None = None,
    ) -> None:
        self._result = result
        self._error = error

    def execute(self, sql: str) -> ExecutionResult:  # noqa: ARG002
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _RecordingAuditStore:
    """In-memory audit store capturing every persisted record (no live DB)."""

    def __init__(self) -> None:
        self.records: list[SqlLabAuditRecord] = []

    def persist(self, record: SqlLabAuditRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# User identity as supplied by the validated JWT (sub/email-like tokens).
_user_identity = st.text(
    alphabet=string.ascii_letters + string.digits + "@._-+",
    min_size=1,
    max_size=40,
)

# SQL strings across the truncation boundary: small statements, and strings at
# and beyond AUDIT_MAX_SQL_LENGTH so the <= 10000-char truncation (R8.5) is
# exercised in both directions.
_short_sql = st.text(min_size=1, max_size=200)
_around_limit_sql = st.integers(
    min_value=AUDIT_MAX_SQL_LENGTH - 5, max_value=AUDIT_MAX_SQL_LENGTH + 500
).map(lambda n: "a" * n)
_sql = st.one_of(_short_sql, _around_limit_sql)

# Rows/columns for the success outcome. Kept small so trimming to Row_Limit is
# not the focus here (that is Property 8); we only need a valid ExecutionResult.
_columns = st.lists(_user_identity, min_size=0, max_size=3, unique=True)


@st.composite
def _success_execution_result(draw: st.DrawFn) -> ExecutionResult:
    columns = draw(_columns)
    row_count = draw(st.integers(min_value=0, max_value=5))
    rows = [{col: idx for col in columns} for idx in range(row_count)]
    duration_ms = draw(st.integers(min_value=0, max_value=5000))
    return ExecutionResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        duration_ms=duration_ms,
        truncated=False,
    )


# A post-guard executor failure: any SqlLabError subclass.
_executor_error = st.one_of(
    st.builds(SqlLabExecutionError, st.just("db boom")),
    st.builds(SqlLabTimeoutError, st.just("timed out")),
    st.builds(SqlLabConnectionError, st.just("connect failed")),
    st.builds(SqlLabConfigError, st.just("missing SQL_VIEWER_DB_USER")),
)


@st.composite
def _scenario(draw: st.DrawFn) -> dict[str, object]:
    """Generate a (user, sql, outcome) scenario driving one of the three paths."""
    user = draw(_user_identity)
    sql = draw(_sql)
    kind = draw(st.sampled_from(["success", "error", "rejected"]))
    scenario: dict[str, object] = {"user": user, "sql": sql, "kind": kind}
    if kind == "success":
        scenario["result"] = draw(_success_execution_result())
    elif kind == "error":
        scenario["error"] = draw(_executor_error)
    return scenario


def _service_for(scenario: dict[str, object], store: _RecordingAuditStore) -> SqlLabService:
    kind = scenario["kind"]
    guard = _FakeGuard(reject=(kind == "rejected"))
    if kind == "success":
        executor = _FakeExecutor(result=scenario["result"])  # type: ignore[arg-type]
    elif kind == "error":
        executor = _FakeExecutor(error=scenario["error"])  # type: ignore[arg-type]
    else:
        executor = _FakeExecutor()  # never reached for a rejection
    return SqlLabService(
        _build_settings(),
        guard=guard,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        audit_store=store,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 12: Each request produces exactly one well-formed audit record
# Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5
@settings(max_examples=300)
@given(scenario=_scenario())
def test_each_request_produces_exactly_one_well_formed_audit_record(
    scenario: dict[str, object],
) -> None:
    """Every outcome persists exactly one well-formed audit record."""
    store = _RecordingAuditStore()
    service = _service_for(scenario, store)
    user = scenario["user"]
    sql = scenario["sql"]
    kind = scenario["kind"]

    if kind == "success":
        service.run(sql, user)  # type: ignore[arg-type]
    elif kind == "error":
        with pytest.raises(SqlLabError):
            service.run(sql, user)  # type: ignore[arg-type]
    else:
        with pytest.raises(SqlLabValidationError):
            service.run(sql, user)  # type: ignore[arg-type]

    # Exactly one audit record is persisted per request outcome (R8.1-R8.3).
    assert len(store.records) == 1
    record = store.records[0]

    # Carries the requesting user identity from the JWT (R8.4).
    assert record.user_identity == user

    # Stores the submitted SQL truncated to <= 10000 characters (R8.5).
    assert record.sql == sql[:AUDIT_MAX_SQL_LENGTH]  # type: ignore[index]
    assert len(record.sql) <= AUDIT_MAX_SQL_LENGTH

    # created_at is a timezone-aware UTC timestamp (R8.1-R8.3).
    assert record.created_at.tzinfo is not None
    assert record.created_at.utcoffset() == timedelta(0)

    # The correct outcome, with the duration/row-count presence rules.
    assert record.outcome == kind
    if kind == "success":
        assert record.duration_ms is not None
        assert record.row_count is not None
    elif kind == "rejected":
        # A rejection carries neither a duration nor a row count (R8.3).
        assert record.duration_ms is None
        assert record.row_count is None
    else:  # error
        assert record.row_count is None
