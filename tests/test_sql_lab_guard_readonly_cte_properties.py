"""Property test for the SQL Lab guard read-only CTE handling.

# Feature: sql-lab, Property 19: Read-only CTEs are allowed and any nested data-modification is rejected

Property statement:

    With ``allow_cte=True``, a single ``SELECT`` whose ``WITH`` clause and all
    nested sub-queries are read-only sub-selects is allowed; any data-modifying
    operation at any level is rejected with a descriptive message.

Two generators exercise the two halves of the property:

* :func:`_read_only_with` builds a single ``SELECT`` rooted statement whose
  ``WITH`` clause defines one or more read-only CTEs (each an ordinary
  sub-select, optionally containing a nested derived-table sub-select) and whose
  final ``SELECT`` reads from one of those CTEs. Every referenced identifier
  carries an ``x_`` prefix so it can never collide with a denylisted sensitive
  table. Each such input must be *accepted* by
  ``SqlLabGuard({"users", "refresh_tokens"}, allow_cte=True).validate`` (which
  returns the normalized SQL).
* :func:`_data_modifying_with` builds a statement whose root is still a
  ``SELECT`` but whose ``WITH`` clause contains a data-modifying CTE — a
  Postgres ``INSERT``/``UPDATE``/``DELETE ... RETURNING *`` wrapped in a common
  table expression — placed at an arbitrary position among read-only CTEs. Each
  such input must be *rejected* with a :class:`SqlLabValidationError` whose
  message indicates that data-modifying operations are not permitted.

**Validates: Requirements 11.6, 11.7**
"""

from __future__ import annotations

import string

import pytest
import sqlglot
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlglot import exp

from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError

# The denylist the guard is constructed with. Generated identifiers never
# collide with these because every identifier is emitted with an ``x_`` prefix.
_SENSITIVE_TABLES = frozenset({"users", "refresh_tokens"})


def _guard() -> SqlLabGuard:
    """A guard with read-only CTE support enabled (Slice 5)."""
    return SqlLabGuard(_SENSITIVE_TABLES, allow_cte=True)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# An identifier guaranteed to be neither a SQL keyword nor a denylisted
# sensitive table, thanks to the leading ``x_``.
_identifier = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6).map(
    lambda s: "x_" + s
)


# ---------------------------------------------------------------------------
# Read-only WITH statements (accepted) — Requirement 11.6
# ---------------------------------------------------------------------------


@st.composite
def _read_only_subselect(draw: st.DrawFn) -> str:
    """A read-only sub-select, optionally wrapping a nested derived-table sub-select."""
    source = draw(_identifier)
    if draw(st.booleans()):
        column = draw(_identifier)
        # A nested read-only sub-select inside a derived table.
        return f"SELECT {column} FROM (SELECT * FROM {source}) x_inner"
    return f"SELECT * FROM {source}"


@st.composite
def _read_only_with(draw: st.DrawFn) -> str:
    """A single SELECT rooted statement with one or more read-only CTEs.

    The ``WITH`` clause defines between one and three read-only CTEs (each an
    ordinary sub-select, optionally nesting a derived-table sub-select) and the
    final ``SELECT`` reads from one of them.
    """
    count = draw(st.integers(min_value=1, max_value=3))
    names = draw(
        st.lists(_identifier, min_size=count, max_size=count, unique=True)
    )
    ctes = [f"{name} AS ({draw(_read_only_subselect())})" for name in names]
    final_ref = draw(st.sampled_from(names))
    return "WITH " + ", ".join(ctes) + f" SELECT * FROM {final_ref}"


# ---------------------------------------------------------------------------
# Data-modifying WITH statements (rejected) — Requirement 11.7
# ---------------------------------------------------------------------------


@st.composite
def _data_modifying_with(draw: st.DrawFn) -> str:
    """A SELECT-rooted statement whose WITH clause hides a data-modifying CTE.

    A data-modifying CTE (Postgres ``INSERT``/``UPDATE``/``DELETE ... RETURNING
    *``) is placed at an arbitrary position among zero or more read-only CTEs.
    The statement root remains a ``SELECT`` that reads from the data-modifying
    CTE, so it is only the nested data modification that must trigger rejection.
    """
    count = draw(st.integers(min_value=1, max_value=3))
    names = draw(
        st.lists(_identifier, min_size=count, max_size=count, unique=True)
    )
    mod_name = names[0]

    table = draw(_identifier)
    column = draw(_identifier)
    value = draw(st.integers(min_value=0, max_value=10_000))
    kind = draw(st.sampled_from(["INSERT", "UPDATE", "DELETE"]))
    if kind == "INSERT":
        body = f"INSERT INTO {table} ({column}) VALUES ({value}) RETURNING *"
    elif kind == "UPDATE":
        body = f"UPDATE {table} SET {column} = {value} RETURNING *"
    else:
        body = f"DELETE FROM {table} WHERE {column} = {value} RETURNING *"

    ctes = [f"{mod_name} AS ({body})"]
    for name in names[1:]:
        ctes.append(f"{name} AS (SELECT * FROM {draw(_identifier)})")

    # Place the data-modifying CTE at an arbitrary position among the others.
    ordered = draw(st.permutations(ctes))
    return "WITH " + ", ".join(ordered) + f" SELECT * FROM {mod_name}"


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 19: Read-only CTEs are allowed and any nested data-modification is rejected
# Validates: Requirements 11.6
@settings(max_examples=300)
@given(sql=_read_only_with())
@example(sql="WITH x_c AS (SELECT * FROM x_s) SELECT * FROM x_c")
@example(sql="WITH x_a AS (SELECT * FROM x_s), x_b AS (SELECT x_v FROM x_a) SELECT * FROM x_b")
@example(
    sql="WITH x_c AS (SELECT x_v FROM (SELECT * FROM x_s) x_inner) SELECT * FROM x_c"
)
def test_read_only_ctes_are_allowed(sql: str) -> None:
    """A single SELECT over read-only CTEs is accepted and its normalized SQL returned."""
    guard = _guard()

    normalized = guard.validate(sql)

    # The guard returns the normalized SQL as a non-empty string.
    assert isinstance(normalized, str)
    assert normalized.strip()

    # The normalized SQL is still exactly one SELECT-rooted statement with a WITH.
    parsed = [
        stmt for stmt in sqlglot.parse(normalized, read="postgres") if stmt is not None
    ]
    assert len(parsed) == 1
    assert isinstance(parsed[0], exp.Select)
    assert next(parsed[0].find_all(exp.With), None) is not None

    # No data-modifying node survived anywhere in the accepted tree.
    assert next(parsed[0].find_all(exp.Insert, exp.Update, exp.Delete), None) is None

    # Validation is idempotent: re-validating the normalized SQL is a no-op.
    assert guard.validate(normalized) == normalized


# Feature: sql-lab, Property 19: Read-only CTEs are allowed and any nested data-modification is rejected
# Validates: Requirements 11.7
@settings(max_examples=300)
@given(sql=_data_modifying_with())
@example(sql="WITH x_c AS (INSERT INTO x_t (x_a) VALUES (1) RETURNING *) SELECT * FROM x_c")
@example(sql="WITH x_c AS (UPDATE x_t SET x_a = 1 RETURNING *) SELECT * FROM x_c")
@example(sql="WITH x_c AS (DELETE FROM x_t WHERE x_a = 1 RETURNING *) SELECT * FROM x_c")
@example(
    sql=(
        "WITH x_r AS (SELECT * FROM x_s), "
        "x_c AS (INSERT INTO x_t (x_a) VALUES (1) RETURNING *) SELECT * FROM x_c"
    )
)
def test_data_modifying_ctes_are_rejected(sql: str) -> None:
    """A data-modifying CTE at any level is rejected as a data-modifying operation."""
    with pytest.raises(SqlLabValidationError) as exc_info:
        _guard().validate(sql)
    # The message must indicate data-modifying operations are not permitted.
    assert "data-modifying" in str(exc_info.value)
