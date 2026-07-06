"""Property test for the SQL Lab guard allowing a single read-only SELECT.

# Feature: sql-lab, Property 2: A single read-only SELECT (including `SELECT *`) is allowed

Property statement:

    *For any* input that, after comment removal, is exactly one read-only
    ``SELECT`` statement (with or without a star projection, with or without a
    single trailing semicolon, and referencing no denylisted table), the guard
    classifies it as allowed and returns the normalized SQL.

The generator below builds syntactically valid single ``SELECT`` statements —
star projections, explicit column lists, optional ``FROM``/``WHERE`` clauses,
optionally wrapped in real (string-literal-free) comments and optionally
terminated by a single trailing semicolon — while guaranteeing that no
referenced identifier collides with a denylisted sensitive table (every
generated identifier carries an ``x_`` prefix). Each such input must be
accepted by :meth:`SqlLabGuard.validate`, which returns the normalized SQL.

**Validates: Requirements 3.2, 3.3**
"""

from __future__ import annotations

import string

import sqlglot
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlglot import exp

from rag_system.sql_lab.guard import SqlLabGuard

# The denylist the guard is constructed with. Generated identifiers never
# collide with these because every identifier is emitted with an ``x_`` prefix.
_SENSITIVE_TABLES = frozenset({"users", "refresh_tokens"})


def _guard() -> SqlLabGuard:
    return SqlLabGuard(_SENSITIVE_TABLES)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# An identifier that is guaranteed to be neither a SQL keyword nor a denylisted
# sensitive table, thanks to the leading ``x_``.
_identifier = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6).map(
    lambda s: "x_" + s
)

# Comment body text that never contains a comment terminator (``*/``), a newline,
# or a quote, so it can be embedded in a real comment without changing structure.
_comment_body = st.text(alphabet=string.ascii_letters + string.digits + " ", max_size=12)

# A projection: a star, or a non-empty list of column identifiers.
_projection = st.one_of(
    st.just("*"),
    st.lists(_identifier, min_size=1, max_size=3, unique=True).map(", ".join),
)


@st.composite
def _read_only_select(draw: st.DrawFn) -> str:
    """Build a syntactically valid single read-only SELECT statement.

    Optionally decorated with a leading block comment, a trailing line comment,
    and a single trailing semicolon — all of which the guard strips before
    classifying.
    """
    projection = draw(_projection)
    parts = ["SELECT", projection]

    include_from = draw(st.booleans())
    if include_from:
        parts += ["FROM", draw(_identifier)]
        if draw(st.booleans()):
            column = draw(_identifier)
            value = draw(st.integers(min_value=0, max_value=10_000))
            parts += ["WHERE", f"{column} = {value}"]

    sql = " ".join(parts)

    # Optional real comments (string-literal free) around the statement.
    if draw(st.booleans()):
        sql = f"/*{draw(_comment_body)}*/ {sql}"

    # Optional single trailing semicolon.
    if draw(st.booleans()):
        sql += ";"

    # Optional trailing line comment on its own line (stripped by the guard).
    if draw(st.booleans()):
        sql += f"\n-- {draw(_comment_body)}"

    return sql


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 2: A single read-only SELECT (including `SELECT *`) is allowed
# Validates: Requirements 3.2, 3.3
@settings(max_examples=300)
@given(sql=_read_only_select())
@example(sql="SELECT *")
@example(sql="SELECT * FROM x_orders")
@example(sql="SELECT * FROM x_orders;")
@example(sql="/* pick everything */ SELECT * FROM x_orders")
@example(sql="SELECT x_a, x_b FROM x_orders WHERE x_a = 1 -- trailing note")
@example(sql="SELECT 1;")
def test_single_read_only_select_is_allowed(sql: str) -> None:
    """A single read-only SELECT is accepted and its normalized SQL returned."""
    guard = _guard()

    normalized = guard.validate(sql)

    # The guard returns the normalized SQL as a non-empty string.
    assert isinstance(normalized, str)
    assert normalized.strip()

    # Normalization strips comments and the single optional trailing semicolon.
    assert not normalized.rstrip().endswith(";")

    # The normalized SQL is still exactly one SELECT statement.
    parsed = [stmt for stmt in sqlglot.parse(normalized, read="postgres") if stmt is not None]
    assert len(parsed) == 1
    assert isinstance(parsed[0], exp.Select)

    # Validation is idempotent: re-validating the normalized SQL is a no-op.
    assert guard.validate(normalized) == normalized
