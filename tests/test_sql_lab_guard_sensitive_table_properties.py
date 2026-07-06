"""Property test for the SQL Lab guard rejecting denylisted sensitive tables.

# Feature: sql-lab, Property 5: Denylisted sensitive-table references are rejected before execution

Property statement:

    *For any* ``SELECT`` statement that references at least one denylisted
    Sensitive_Table (in any table position or qualifier), the guard rejects the
    statement before execution and the executor is never invoked.

The generator below builds syntactically valid single ``SELECT`` statements that
reference a denylisted sensitive table in one of several positions — the ``FROM``
clause, a ``JOIN``, a subquery in the ``FROM`` clause, or a column qualifier
(``users.id``). Non-sensitive identifiers always carry an ``x_`` prefix so the
only denylisted reference in each statement is the intentionally injected one.
Each such input must be rejected by :meth:`SqlLabGuard.validate` with a
``SqlLabValidationError`` whose message names the sensitive-table reference.

**Validates: Requirements 2.4**
"""

from __future__ import annotations

import string

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError

# The denylist the guard is constructed with. Generated non-sensitive
# identifiers never collide with these because they carry an ``x_`` prefix.
_SENSITIVE_TABLES = frozenset({"users", "refresh_tokens"})


def _guard() -> SqlLabGuard:
    return SqlLabGuard(_SENSITIVE_TABLES)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A non-sensitive identifier: guaranteed distinct from any denylisted table by
# the leading ``x_``.
_identifier = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6).map(
    lambda s: "x_" + s
)

# A denylisted sensitive-table name, drawn from the configured denylist. Case is
# varied to confirm case-insensitive matching.
_sensitive_table = st.sampled_from(sorted(_SENSITIVE_TABLES)).flatmap(
    lambda name: st.sampled_from([name, name.upper(), name.capitalize()])
)


@st.composite
def _select_referencing_sensitive_table(draw: st.DrawFn) -> str:
    """Build a single SELECT that references a denylisted table in some position."""
    sensitive = draw(_sensitive_table)
    position = draw(
        st.sampled_from(["from", "join", "subquery", "column_qualifier"])
    )

    if position == "from":
        # SELECT * FROM <sensitive>
        return f"SELECT * FROM {sensitive}"

    if position == "join":
        # SELECT * FROM x_other JOIN <sensitive> ON ...
        other = draw(_identifier)
        return (
            f"SELECT * FROM {other} "
            f"JOIN {sensitive} ON {other}.id = {sensitive}.id"
        )

    if position == "subquery":
        # SELECT * FROM (SELECT * FROM <sensitive>) t
        alias = draw(_identifier)
        return f"SELECT * FROM (SELECT * FROM {sensitive}) {alias}"

    # column_qualifier: SELECT <sensitive>.col FROM x_other
    #
    # Reference the sensitive table only through a column qualifier, joined from
    # a non-sensitive table position so the qualifier is the sole denylisted
    # reference.
    other = draw(_identifier)
    column = draw(_identifier)
    return f"SELECT {sensitive}.{column} FROM {other}"


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 5: Denylisted sensitive-table references are rejected before execution
# Validates: Requirements 2.4
@settings(max_examples=300)
@given(sql=_select_referencing_sensitive_table())
@example(sql="SELECT * FROM users")
@example(sql="SELECT * FROM refresh_tokens")
@example(sql="SELECT * FROM x_orders JOIN users ON x_orders.id = users.id")
@example(sql="SELECT * FROM (SELECT * FROM users) t")
@example(sql="SELECT users.id FROM x_orders")
@example(sql="SELECT * FROM USERS")
def test_sensitive_table_reference_is_rejected(sql: str) -> None:
    """A SELECT referencing a denylisted table is rejected before execution."""
    guard = _guard()

    with pytest.raises(SqlLabValidationError) as exc_info:
        guard.validate(sql)

    # The rejection message names the sensitive-table reason so the caller can
    # surface why the query was blocked.
    message = str(exc_info.value)
    assert "Sensitive-table reference" in message
    # The message names at least one denylisted table (lower-cased in the guard).
    assert any(table in message for table in _SENSITIVE_TABLES)
