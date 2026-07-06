"""Property test: a guard-rejected statement is never executed (task 4.3).

# Feature: sql-lab, Property 4: A rejected statement is never executed

Property statement:

    *For any* input the guard rejects (for any reason, including
    empty/whitespace and guard-disallowed statements), the executor is never
    invoked and no statement is run against the database, so database state is
    left unchanged.

:class:`~rag_system.sql_lab.service.SqlLabService` orchestrates guard → execute:
the submitted statement is handed to the guard *before* anything touches the
database, and only a guard-approved statement is passed to the executor. This
test wires a real :class:`~rag_system.sql_lab.guard.SqlLabGuard` (so genuine
rejection logic runs) together with a **spy executor** that records whether its
``execute`` method was ever called and fails the test immediately if it is.

For every generated input drawn from one of the guard's rejection categories —
empty/whitespace, write/DDL/administrative statements, multiple statements,
``WITH`` clauses, unparseable text, and denylisted sensitive-table references —
the test asserts that :meth:`SqlLabService.run` raises
:class:`~rag_system.sql_lab.guard.SqlLabValidationError` and that the spy
executor was never invoked, proving the database is never touched for a rejected
statement (R3.10, R4.11, R4.12).

**Validates: Requirements 3.10, 4.11, 4.12**
"""

from __future__ import annotations

import string

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.sql_lab.executor import ExecutionResult
from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError
from rag_system.sql_lab.service import SqlLabService

# The denylist the guard is constructed with. Generated non-sensitive
# identifiers never collide with these because they carry an ``x_`` prefix.
_SENSITIVE_TABLES = frozenset({"users", "refresh_tokens"})

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


class _SpyExecutor:
    """Executor stub that must never be invoked for a rejected statement.

    ``execute`` records the call and fails the test immediately, so any path in
    which a guard-rejected statement reaches the database is caught. It also
    exposes a ``called`` flag the test asserts on directly.
    """

    def __init__(self) -> None:
        self.called = False

    def execute(self, sql: str) -> ExecutionResult:  # noqa: ARG002
        self.called = True
        raise AssertionError(
            "SqlLabExecutor.execute must never run for a guard-rejected statement"
        )


class _RecordingAuditStore:
    """In-memory audit store so the service records the rejection without a DB.

    A guard rejection persists exactly one ``rejected`` audit record before the
    error re-raises; this fake captures it so no live database connection is
    attempted during the property run.
    """

    def __init__(self) -> None:
        self.records: list[object] = []

    def persist(self, record: object) -> None:
        self.records.append(record)


def _service(executor: _SpyExecutor) -> SqlLabService:
    """A service wiring a real guard, the spy executor, and a fake audit store."""
    return SqlLabService(
        _build_settings(),
        guard=SqlLabGuard(_SENSITIVE_TABLES),
        executor=executor,  # type: ignore[arg-type]
        audit_store=_RecordingAuditStore(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Strategies — one per guard rejection category (mirrors Properties 3 and 5).
# ---------------------------------------------------------------------------

# A non-sensitive identifier: guaranteed neither a SQL keyword nor a denylisted
# table thanks to the leading ``x_``.
_identifier = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6).map(
    lambda s: "x_" + s
)


@st.composite
def _write_statement(draw: st.DrawFn) -> str:
    """A single write / data-modifying statement (INSERT/UPDATE/DELETE/MERGE/TRUNCATE)."""
    table = draw(_identifier)
    column = draw(_identifier)
    value = draw(st.integers(min_value=0, max_value=10_000))
    other = draw(_identifier)
    kind = draw(st.sampled_from(["INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"]))
    if kind == "INSERT":
        return f"INSERT INTO {table} ({column}) VALUES ({value})"
    if kind == "UPDATE":
        return f"UPDATE {table} SET {column} = {value}"
    if kind == "DELETE":
        return f"DELETE FROM {table} WHERE {column} = {value}"
    if kind == "MERGE":
        return (
            f"MERGE INTO {table} USING {other} ON {table}.{column} = {other}.{column} "
            f"WHEN MATCHED THEN UPDATE SET {column} = {value}"
        )
    return f"TRUNCATE TABLE {table}"


@st.composite
def _ddl_statement(draw: st.DrawFn) -> str:
    """A single DDL / administrative statement (CREATE/ALTER/DROP/GRANT/REVOKE/COPY/SET/VACUUM)."""
    table = draw(_identifier)
    column = draw(_identifier)
    role = draw(_identifier)
    kind = draw(
        st.sampled_from(
            ["CREATE", "ALTER", "DROP", "GRANT", "REVOKE", "COPY", "SET", "VACUUM"]
        )
    )
    if kind == "CREATE":
        return f"CREATE TABLE {table} ({column} INT)"
    if kind == "ALTER":
        return f"ALTER TABLE {table} ADD COLUMN {column} INT"
    if kind == "DROP":
        return f"DROP TABLE {table}"
    if kind == "GRANT":
        return f"GRANT SELECT ON {table} TO {role}"
    if kind == "REVOKE":
        return f"REVOKE SELECT ON {table} FROM {role}"
    if kind == "COPY":
        return f"COPY {table} TO STDOUT"
    if kind == "SET":
        return f"SET {column} = 1"
    return f"VACUUM {table}"


@st.composite
def _multiple_statements(draw: st.DrawFn) -> str:
    """More than one statement separated by a semicolon (beyond one trailing ``;``)."""
    table_a = draw(_identifier)
    table_b = draw(_identifier)
    first = f"SELECT * FROM {table_a}"
    second = draw(
        st.sampled_from([f"SELECT * FROM {table_b}", f"SELECT {draw(_identifier)}"])
    )
    trailing = draw(st.sampled_from(["", ";"]))
    return f"{first}; {second}{trailing}"


@st.composite
def _with_statement(draw: st.DrawFn) -> str:
    """A read-only SELECT that uses a WITH clause (rejected in v1)."""
    cte = draw(_identifier)
    source = draw(_identifier)
    return f"WITH {cte} AS (SELECT * FROM {source}) SELECT * FROM {cte}"


_unparseable = st.sampled_from(
    [
        "SELECT FROM WHERE",
        "SELECT * FROM (",
        "SELECT * FROM x_t WHERE",
        "))) SELECT",
        "SELECT * FROM x_t GROUP BY )",
        "SELECT * x_t FROM WHERE ORDER",
    ]
)

_empty_or_whitespace = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r", max_size=8),
    st.text(alphabet=" \t\n\r", max_size=4).map(lambda ws: f"{ws}-- only a comment{ws}"),
    st.text(alphabet=" \t\n\r", max_size=4).map(lambda ws: f"{ws}/* block only */{ws}"),
)

_sensitive_table = st.sampled_from(sorted(_SENSITIVE_TABLES)).flatmap(
    lambda name: st.sampled_from([name, name.upper(), name.capitalize()])
)


@st.composite
def _select_referencing_sensitive_table(draw: st.DrawFn) -> str:
    """A single SELECT that references a denylisted table in some position."""
    sensitive = draw(_sensitive_table)
    position = draw(st.sampled_from(["from", "join", "subquery", "column_qualifier"]))
    if position == "from":
        return f"SELECT * FROM {sensitive}"
    if position == "join":
        other = draw(_identifier)
        return f"SELECT * FROM {other} JOIN {sensitive} ON {other}.id = {sensitive}.id"
    if position == "subquery":
        alias = draw(_identifier)
        return f"SELECT * FROM (SELECT * FROM {sensitive}) {alias}"
    other = draw(_identifier)
    column = draw(_identifier)
    return f"SELECT {sensitive}.{column} FROM {other}"


# Any input the guard rejects, drawn from every rejection category.
_rejected_input = st.one_of(
    _empty_or_whitespace,
    _write_statement(),
    _ddl_statement(),
    _multiple_statements(),
    _with_statement(),
    _unparseable,
    _select_referencing_sensitive_table(),
)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 4: A rejected statement is never executed
# Validates: Requirements 3.10, 4.11, 4.12
@settings(max_examples=400)
@given(sql=_rejected_input)
@example(sql="")
@example(sql="   ")
@example(sql="-- only a comment")
@example(sql="INSERT INTO x_t (x_a) VALUES (1)")
@example(sql="UPDATE x_t SET x_a = 1")
@example(sql="DROP TABLE x_t")
@example(sql="SELECT * FROM x_a; SELECT * FROM x_b")
@example(sql="WITH x_c AS (SELECT * FROM x_s) SELECT * FROM x_c")
@example(sql="SELECT FROM WHERE")
@example(sql="SELECT * FROM users")
def test_rejected_statement_is_never_executed(sql: str) -> None:
    """A guard-rejected statement raises and never reaches the executor."""
    spy = _SpyExecutor()
    service = _service(spy)

    # The guard rejection must propagate out of the service unchanged.
    with pytest.raises(SqlLabValidationError):
        service.run(sql, "operator@example.com")

    # And, crucially, the executor was never invoked — nothing ran against the
    # database, so database state is left unchanged (R3.10, R4.11, R4.12).
    assert spy.called is False
