"""Dangerous-function denylist for both SQL guards (finding #6).

The guards previously validated only statement shape and table/column names, so
resource-exhaustion and filesystem/network/large-object/session-config functions
(``pg_sleep``, ``pg_read_file``, ``lo_get``, ``dblink``, ``current_setting``,
``set_config`` …) passed straight through. These now fail closed at both the SQL
Lab guard and the copilot guard, while ordinary aggregates/scalars still pass.
"""

from __future__ import annotations

import pytest

from rag_system.copilot import (
    CopilotSchemaCatalog,
    CopilotSqlGuard,
    CopilotTable,
    CopilotColumn,
    SqlValidationError,
    find_denied_function,
)
from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError
import sqlglot

_DENIED = [
    "SELECT pg_sleep(100)",
    "SELECT current_setting('search_path')",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT * FROM pg_read_file('/etc/passwd')",
    "SELECT lo_get(1)",
    "SELECT dblink('host=evil', 'select 1')",
    "SELECT pg_ls_dir('/')",
]


# ---------------------------------------------------------------------------
# Shared helper.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", _DENIED)
def test_find_denied_function_flags_dangerous_calls(sql: str) -> None:
    statement = sqlglot.parse_one(sql, read="postgres")
    assert find_denied_function(statement) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM t",
        "SELECT lower(name), sum(amount) FROM t GROUP BY 1",
        "SELECT max(created_at) FROM t",
    ],
)
def test_find_denied_function_allows_ordinary_functions(sql: str) -> None:
    statement = sqlglot.parse_one(sql, read="postgres")
    assert find_denied_function(statement) is None


# ---------------------------------------------------------------------------
# SQL Lab guard (allows detail SELECTs / SELECT *).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", _DENIED)
def test_sql_lab_guard_rejects_denied_functions(sql: str) -> None:
    guard = SqlLabGuard(sensitive_tables=["users", "refresh_tokens"])
    with pytest.raises(SqlLabValidationError) as exc:
        guard.validate(sql)
    assert "Disallowed function" in str(exc.value)


def test_sql_lab_guard_still_allows_plain_select() -> None:
    guard = SqlLabGuard(sensitive_tables=["users"])
    assert guard.validate("SELECT id, name FROM products") == "SELECT id, name FROM products"


# ---------------------------------------------------------------------------
# Copilot guard (requires an aggregate; reuses the same denylist).
# ---------------------------------------------------------------------------


def _catalog() -> CopilotSchemaCatalog:
    return CopilotSchemaCatalog(
        tables=[
            CopilotTable(
                name="sales_order",
                columns=[CopilotColumn(name="amount"), CopilotColumn(name="region")],
            )
        ]
    )


def test_copilot_guard_rejects_denied_function_in_aggregate() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=100)
    # pg_sleep smuggled alongside a valid aggregate must still be rejected.
    with pytest.raises(SqlValidationError) as exc:
        guard.validate("SELECT sum(amount), pg_sleep(10) FROM sales_order")
    assert "Disallowed function" in str(exc.value)


def test_copilot_guard_allows_ordinary_aggregate() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=100)
    out = guard.validate("SELECT region, sum(amount) FROM sales_order GROUP BY region")
    assert "sum(amount)" in out.lower() or "sum(amount)" in out
