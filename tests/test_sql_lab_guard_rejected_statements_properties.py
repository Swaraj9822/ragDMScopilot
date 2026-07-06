"""Property test for the SQL Lab guard rejecting non-read-only-SELECT input.

# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason

Property statement:

    *For any* input that, after comment removal, is not a single read-only
    ``SELECT`` — i.e. it contains a write/data-modifying operation
    (``INSERT``/``UPDATE``/``DELETE``/``MERGE``/``TRUNCATE``), a
    DDL/administrative operation (``CREATE``/``ALTER``/``DROP``/``GRANT``/
    ``REVOKE``/``COPY``/``SET``/``VACUUM``/any other non-``SELECT`` command),
    more than one statement (excluding one optional trailing semicolon), a
    ``WITH`` clause (in v1), unparseable text, or is empty/whitespace-only — the
    guard rejects it with a message that identifies the specific rejection
    reason.

Each strategy below generates inputs drawn from one of the rejection
categories. Every generated input must cause :meth:`SqlLabGuard.validate` to
raise :class:`SqlLabValidationError`, and the message must name the specific
rejection reason for that category.

**Validates: Requirements 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**
"""

from __future__ import annotations

import string

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError

# The denylist the guard is constructed with. Generated identifiers never
# collide with these because every identifier is emitted with an ``x_`` prefix.
_SENSITIVE_TABLES = frozenset({"users", "refresh_tokens"})


def _guard() -> SqlLabGuard:
    return SqlLabGuard(_SENSITIVE_TABLES)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# An identifier guaranteed to be neither a SQL keyword nor a denylisted
# sensitive table, thanks to the leading ``x_``.
_identifier = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6).map(
    lambda s: "x_" + s
)


# ---------------------------------------------------------------------------
# Category 1: write / data-modifying operations (R3.4)
# ---------------------------------------------------------------------------


@st.composite
def _write_statement(draw: st.DrawFn) -> str:
    """A single write / data-modifying statement (INSERT/UPDATE/DELETE/MERGE/TRUNCATE)."""
    table = draw(_identifier)
    column = draw(_identifier)
    value = draw(st.integers(min_value=0, max_value=10_000))
    other = draw(_identifier)
    kind = draw(
        st.sampled_from(["INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"])
    )
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


# ---------------------------------------------------------------------------
# Category 2: DDL / administrative operations (R3.5)
# ---------------------------------------------------------------------------


@st.composite
def _ddl_statement(draw: st.DrawFn) -> str:
    """A single DDL / administrative statement (CREATE/ALTER/DROP/GRANT/REVOKE/COPY/SET/VACUUM)."""
    table = draw(_identifier)
    column = draw(_identifier)
    role = draw(_identifier)
    kind = draw(
        st.sampled_from(
            [
                "CREATE",
                "ALTER",
                "DROP",
                "GRANT",
                "REVOKE",
                "COPY",
                "SET",
                "VACUUM",
            ]
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


# ---------------------------------------------------------------------------
# Category 3: multiple statements (R3.6)
# ---------------------------------------------------------------------------


@st.composite
def _multiple_statements(draw: st.DrawFn) -> str:
    """More than one statement separated by a semicolon (beyond one trailing ``;``)."""
    table_a = draw(_identifier)
    table_b = draw(_identifier)
    # At least two real statements. A single optional trailing ``;`` is allowed
    # by the guard, so we ensure a second statement follows the separator.
    first = f"SELECT * FROM {table_a}"
    second = draw(
        st.sampled_from(
            [
                f"SELECT * FROM {table_b}",
                f"SELECT {draw(_identifier)}",
            ]
        )
    )
    trailing = draw(st.sampled_from(["", ";"]))
    return f"{first}; {second}{trailing}"


# ---------------------------------------------------------------------------
# Category 4: WITH clause / CTE (R3.7)
# ---------------------------------------------------------------------------


@st.composite
def _with_statement(draw: st.DrawFn) -> str:
    """A read-only SELECT that uses a WITH clause (rejected in v1)."""
    cte = draw(_identifier)
    source = draw(_identifier)
    return f"WITH {cte} AS (SELECT * FROM {source}) SELECT * FROM {cte}"


# ---------------------------------------------------------------------------
# Category 5: unparseable text (R3.8)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Category 6: empty / whitespace-only (R3.9)
# ---------------------------------------------------------------------------

# Empty or whitespace-only after comment removal (including comment-only input).
_empty_or_whitespace = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r", max_size=8),
    st.text(alphabet=" \t\n\r", max_size=4).map(lambda ws: f"{ws}-- only a comment{ws}"),
    st.text(alphabet=" \t\n\r", max_size=4).map(lambda ws: f"{ws}/* block only */{ws}"),
)


# ---------------------------------------------------------------------------
# Property tests, one per rejection category. Each asserts the guard raises and
# the message identifies the specific rejection reason for that category.
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason
# Validates: Requirements 3.4
@settings(max_examples=200)
@given(sql=_write_statement())
@example(sql="INSERT INTO x_t (x_a) VALUES (1)")
@example(sql="UPDATE x_t SET x_a = 1")
@example(sql="DELETE FROM x_t WHERE x_a = 1")
@example(sql="TRUNCATE TABLE x_t")
def test_write_operations_are_rejected(sql: str) -> None:
    """Any write / data-modifying statement is rejected naming a disallowed operation."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    assert "Disallowed operation" in str(exc_info.value)


# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason
# Validates: Requirements 3.5
@settings(max_examples=200)
@given(sql=_ddl_statement())
@example(sql="CREATE TABLE x_t (x_a INT)")
@example(sql="DROP TABLE x_t")
@example(sql="GRANT SELECT ON x_t TO x_r")
@example(sql="COPY x_t TO STDOUT")
@example(sql="SET x_a = 1")
@example(sql="VACUUM x_t")
def test_ddl_and_administrative_operations_are_rejected(sql: str) -> None:
    """Any DDL / administrative statement is rejected naming a disallowed operation."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    assert "Disallowed operation" in str(exc_info.value)


# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason
# Validates: Requirements 3.6
@settings(max_examples=200)
@given(sql=_multiple_statements())
@example(sql="SELECT * FROM x_a; SELECT * FROM x_b")
@example(sql="SELECT 1; SELECT 2;")
def test_multiple_statements_are_rejected(sql: str) -> None:
    """More than one statement is rejected naming multiple statements."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    assert "Multiple statements" in str(exc_info.value)


# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason
# Validates: Requirements 3.7
@settings(max_examples=200)
@given(sql=_with_statement())
@example(sql="WITH x_c AS (SELECT * FROM x_s) SELECT * FROM x_c")
def test_with_clause_is_rejected(sql: str) -> None:
    """A WITH clause (CTE) is rejected in v1 naming the WITH clause."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    assert "WITH clause" in str(exc_info.value)


# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason
# Validates: Requirements 3.8
@settings(max_examples=100)
@given(sql=_unparseable)
def test_unparseable_text_is_rejected(sql: str) -> None:
    """Unparseable text is rejected naming the parse failure."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    assert "Parse failure" in str(exc_info.value)


# Feature: sql-lab, Property 3: Every non-(single read-only SELECT) input is rejected with a reason
# Validates: Requirements 3.9
@settings(max_examples=100)
@given(sql=_empty_or_whitespace)
@example(sql="")
@example(sql="   ")
@example(sql="-- only a comment")
@example(sql="/* block only */")
def test_empty_or_whitespace_is_rejected(sql: str) -> None:
    """Empty or whitespace-only input (after comment removal) is rejected naming empty input."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    assert "Empty input" in str(exc_info.value)
