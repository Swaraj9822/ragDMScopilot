"""Property test for SQL Lab Result_Set shaping (task 4.2).

Feature: sql-lab (Slice 1 — service orchestration & Result_Set shaping).

# Feature: sql-lab, Property 8: Result_Set shaping enforces the row limit and truncation flag

Property statement:

    *For any* list of fetched rows and any Row_Limit ``N``, the shaped
    Result_Set returns exactly ``min(produced, N)`` rows, sets ``truncated`` to
    true iff the query produced more than ``N`` rows, reports ``rowCount`` equal
    to the number of returned rows, echoes the submitted ``sql``, exposes
    ``columns``, and reports ``durationMs`` as a non-negative integer —
    regardless of whether the submitted query had its own ``LIMIT``.

:class:`~rag_system.sql_lab.service.SqlLabService` orchestrates guard → execute
→ shape. This test isolates the *shaping* step by injecting

* a **fake guard** whose ``validate`` accepts every input (shaping must hold for
  any statement the guard would have allowed, including ones carrying their own
  ``LIMIT``), and
* a **fake executor** returning a controlled :class:`ExecutionResult` so no
  database is touched.

Faithful to the real executor, which fetches ``Row_Limit + 1`` rows via
``fetchmany`` to detect truncation without a second round-trip, the fake
executor returns at most ``N + 1`` rows for a query that produced ``P`` rows
(``min(P, N + 1)``). The service must then trim to ``min(P, N)`` and set
``truncated`` iff ``P > N``.

**Validates: Requirements 4.5, 4.6, 4.7, 4.8, 4.10**
"""

from __future__ import annotations

from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.sql_lab.executor import ExecutionResult
from rag_system.sql_lab.service import SqlLabService

# Required Settings supplied by alias so the model builds in isolation
# (mirrors the convention in the sibling executor unit/property tests).
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(row_limit: int) -> Settings:
    """Construct ``Settings`` with the given SQL Lab Row_Limit (via its alias)."""
    return Settings(  # type: ignore[arg-type]
        **_REQUIRED_BY_ALIAS,
        SQL_LAB_ROW_LIMIT=row_limit,
    )


class _AcceptingGuard:
    """Fake guard whose ``validate`` accepts every statement.

    Records each validated statement so the test can assert the guard ran
    before the executor. Returns the SQL unchanged, matching the real guard's
    contract of returning the normalized statement on success.
    """

    def __init__(self) -> None:
        self.validated: list[str] = []

    def validate(self, sql: str) -> str:
        self.validated.append(sql)
        return sql


class _FakeExecutor:
    """Fake executor returning a preset :class:`ExecutionResult`.

    Records the statement it was asked to run so the test can assert the service
    executed exactly the guard-approved SQL.
    """

    def __init__(self, result: ExecutionResult) -> None:
        self._result = result
        self.executed: list[str] = []

    def execute(self, sql: str) -> ExecutionResult:
        self.executed.append(sql)
        return self._result


class _RecordingAuditStore:
    """In-memory audit store capturing the success record without a DB.

    The service persists exactly one ``success`` audit record before returning
    the shaped Result_Set; this fake captures it so the shaping property runs
    without a live database connection.
    """

    def __init__(self) -> None:
        self.records: list[object] = []

    def persist(self, record: object) -> None:
        self.records.append(record)


def _make_row(columns: list[str], index: int) -> dict[str, Any]:
    """Build a distinguishable row dict for the given select-order columns."""
    return {column: f"{column}-{index}" for column in columns}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Distinct, select-order column names (may be empty for a value-less projection).
_columns = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8),
    min_size=0,
    max_size=5,
    unique=True,
)

# Submitted SQL — deliberately mixes statements that carry their own LIMIT with
# ones that do not, so shaping must hold "regardless of whether the submitted
# query had its own LIMIT" (R4.8). The fake guard accepts all of them.
_submitted_sql = st.sampled_from(
    [
        "SELECT * FROM orders",
        "SELECT id, name FROM customers",
        "SELECT * FROM orders LIMIT 5",
        "SELECT * FROM orders LIMIT 100000",
        "select a from t where b > 1 order by a",
        "SELECT count(*) FROM events LIMIT 1",
    ]
)


@st.composite
def _shaping_case(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Row_Limit, a produced-row count, columns, duration, and SQL."""
    row_limit = draw(st.integers(min_value=1, max_value=50))
    # Total rows the query *produced* (may far exceed the executor's fetch cap).
    produced = draw(st.integers(min_value=0, max_value=130))
    columns = draw(_columns)
    duration_ms = draw(st.integers(min_value=0, max_value=10_000))
    sql = draw(_submitted_sql)
    return {
        "row_limit": row_limit,
        "produced": produced,
        "columns": columns,
        "duration_ms": duration_ms,
        "sql": sql,
    }


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 8: Result_Set shaping enforces the row limit and truncation flag
# Validates: Requirements 4.5, 4.6, 4.7, 4.8, 4.10
@settings(max_examples=300)
@given(case=_shaping_case())
# P < N (no truncation, fewer than the limit).
@example(case={"row_limit": 10, "produced": 3, "columns": ["a"], "duration_ms": 7, "sql": "SELECT a FROM t"})
# P == N (exactly the limit, no truncation).
@example(case={"row_limit": 10, "produced": 10, "columns": ["a"], "duration_ms": 0, "sql": "SELECT a FROM t"})
# P == N + 1 (one over: the truncation-probe boundary).
@example(case={"row_limit": 10, "produced": 11, "columns": ["a"], "duration_ms": 1, "sql": "SELECT a FROM t LIMIT 5"})
# P >> N (far over the limit).
@example(case={"row_limit": 1, "produced": 130, "columns": [], "duration_ms": 42, "sql": "SELECT * FROM t"})
# P == 0 (empty result set).
@example(case={"row_limit": 5, "produced": 0, "columns": ["x", "y"], "duration_ms": 3, "sql": "SELECT x, y FROM t"})
def test_result_set_shaping_enforces_limit_and_truncation(case: dict[str, Any]) -> None:
    """The shaped Result_Set honours the row limit, truncation, and echoed fields."""
    row_limit: int = case["row_limit"]
    produced: int = case["produced"]
    columns: list[str] = case["columns"]
    duration_ms: int = case["duration_ms"]
    sql: str = case["sql"]

    # Faithful to the real executor: it runs ``fetchmany(row_limit + 1)``, so it
    # returns at most ``N + 1`` rows even when the query produced far more.
    fetched_count = min(produced, row_limit + 1)
    fetched_rows = [_make_row(columns, i) for i in range(fetched_count)]
    execution_result = ExecutionResult(
        columns=columns,
        rows=fetched_rows,
        row_count=len(fetched_rows),
        duration_ms=duration_ms,
        # The service recomputes truncation itself; this mirrors the executor.
        truncated=produced > row_limit,
    )

    guard = _AcceptingGuard()
    executor = _FakeExecutor(execution_result)
    service = SqlLabService(
        _build_settings(row_limit),
        guard=guard,
        executor=executor,
        audit_store=_RecordingAuditStore(),
    )

    shaped = service.run(sql, "operator@example.com")

    expected_returned = min(produced, row_limit)
    expected_truncated = produced > row_limit

    # Exactly min(produced, N) rows, in the original fetched order (R4.6, R4.7).
    assert len(shaped.rows) == expected_returned
    assert shaped.rows == fetched_rows[:row_limit]
    assert shaped.rows == [_make_row(columns, i) for i in range(expected_returned)]

    # truncated is true iff the query produced more than N rows (R4.6, R4.7, R4.8).
    assert shaped.truncated is expected_truncated

    # rowCount equals the number of returned rows (R4.5).
    assert shaped.row_count == expected_returned
    assert shaped.row_count == len(shaped.rows)

    # The submitted SQL is echoed back verbatim (R4.5).
    assert shaped.sql == sql

    # Columns are exposed in select order (R4.5).
    assert shaped.columns == columns

    # durationMs is a non-negative integer (R4.5, R4.10).
    assert isinstance(shaped.duration_ms, int)
    assert shaped.duration_ms >= 0
    assert shaped.duration_ms == duration_ms

    # The guard ran before the executor, and the executor ran the approved SQL.
    assert guard.validated == [sql]
    assert executor.executed == [sql]
