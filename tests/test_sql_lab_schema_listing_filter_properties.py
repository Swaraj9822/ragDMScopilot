"""Property test for the SQL Lab schema-listing filter (task 10.2).

Feature: sql-lab (Slice 2 — schema sidebar / ``GET /sql/schema``).

# Feature: sql-lab, Property 10: Schema listing excludes ungranted and sensitive tables

Property statement:

    The schema listing never includes a table for which the viewer role holds
    no ``SELECT`` grant (in particular sensitive tables like
    ``users``/``refresh_tokens`` never appear).

Design context
--------------
:class:`~rag_system.sql_lab.schema_lister.SqlLabSchemaLister` performs the
actual grant filtering *in SQL*: ``_SCHEMA_QUERY`` restricts
``information_schema.columns`` to tables the connected viewer role holds a
``SELECT`` grant on via ``information_schema.role_table_grants`` (grantee =
``current_user``). Because the viewer role holds no grant on the
Sensitive_Tables, those rows are excluded by the database before they are ever
fetched — the database, not application code, is the boundary (verified live by
the integration test, task 10.3).

The remaining application-side unit is
:meth:`SqlLabSchemaLister._group_rows`, which shapes the already-filtered
``(table_name, column_name, data_type)`` rows into ``SchemaTable`` objects. This
property test isolates that shaping step and verifies it **never introduces a
table the fetched rows did not contain** and **faithfully preserves table
encounter order and in-table column order/membership**. We model the SQL grant
filter by construction: the generated fetched rows only ever contain granted,
non-sensitive tables (exactly what the query would have returned), so any
ungranted or sensitive table is, by construction, absent from the input. The
property then asserts such tables never appear in the grouped output — i.e. the
shaper invents nothing.

**Validates: Requirements 7.3**
"""

from __future__ import annotations

from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.sql_lab.schema_lister import (
    SchemaColumn,
    SchemaTable,
    SqlLabSchemaLister,
)

# Tables the viewer role holds NO SELECT grant on, so the SQL query excludes
# them and they never appear in fetched rows. Includes the Sensitive_Tables.
_SENSITIVE_TABLES = ("users", "refresh_tokens")
_UNGRANTED_TABLES = (*_SENSITIVE_TABLES, "auth_sessions", "api_secrets")

# Identifier alphabet for generated (granted) table and column names. Excludes
# the ungranted/sensitive names above so generated grants never collide with the
# set that must never appear.
_IDENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=10).filter(
    lambda name: name not in _UNGRANTED_TABLES
)

_DATA_TYPES = st.sampled_from(
    ["integer", "text", "boolean", "timestamp", "numeric", "uuid", "jsonb"]
)


@st.composite
def _granted_schema(draw: st.DrawFn) -> list[SchemaTable]:
    """Generate a granted schema: distinct tables, each with ordered columns.

    This mirrors exactly what ``_SCHEMA_QUERY`` would return *after* the grant
    filter: only granted (non-sensitive) tables, each with at least one column,
    columns in ``ordinal_position`` order.
    """
    table_names = draw(
        st.lists(_IDENT, min_size=0, max_size=6, unique=True)
    )
    tables: list[SchemaTable] = []
    for table_name in table_names:
        column_names = draw(
            st.lists(_IDENT, min_size=1, max_size=6, unique=True)
        )
        columns = [
            SchemaColumn(name=column_name, type=draw(_DATA_TYPES))
            for column_name in column_names
        ]
        tables.append(SchemaTable(name=table_name, columns=columns))
    return tables


def _fetched_rows(tables: list[SchemaTable]) -> list[dict[str, Any]]:
    """Flatten granted tables into query-ordered ``information_schema`` rows.

    Matches ``_SCHEMA_QUERY``'s ``ORDER BY table_name, ordinal_position``: rows
    for a table are contiguous and in column order.
    """
    rows: list[dict[str, Any]] = []
    for table in tables:
        for column in table.columns:
            rows.append(
                {
                    "table_name": table.name,
                    "column_name": column.name,
                    "data_type": column.type,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 10: Schema listing excludes ungranted and sensitive tables
# Validates: Requirements 7.3
@settings(max_examples=300)
@given(tables=_granted_schema())
# Empty grant set -> empty listing (no tables invented).
@example(tables=[])
# A single granted table with several columns.
@example(
    tables=[
        SchemaTable(
            name="orders",
            columns=[
                SchemaColumn(name="id", type="integer"),
                SchemaColumn(name="total", type="numeric"),
            ],
        )
    ]
)
def test_schema_listing_excludes_ungranted_and_sensitive_tables(
    tables: list[SchemaTable],
) -> None:
    """``_group_rows`` preserves granted tables exactly and invents nothing.

    Because the SQL grant filter (modelled here by construction) never emits an
    ungranted or sensitive table, none can appear in the grouped listing, and
    the shaper preserves table encounter order plus in-table column order and
    membership verbatim.
    """
    fetched = _fetched_rows(tables)

    grouped = SqlLabSchemaLister._group_rows(fetched)

    listed_names = [table.name for table in grouped]

    # No ungranted or sensitive table can ever appear (the core property, R7.3).
    for forbidden in _UNGRANTED_TABLES:
        assert forbidden not in listed_names

    # The listing invents no table: its table set is exactly the granted set.
    assert set(listed_names) == {table.name for table in tables}

    # Table encounter order is preserved (query orders by table_name; the shaper
    # preserves first-encounter order).
    assert listed_names == [table.name for table in tables]

    # No duplicate tables are produced.
    assert len(listed_names) == len(set(listed_names))

    # Each table's columns are preserved in order and membership, with no
    # invented or dropped columns.
    grouped_by_name = {table.name: table for table in grouped}
    for expected in tables:
        actual = grouped_by_name[expected.name]
        assert actual.columns == expected.columns
        assert [column.name for column in actual.columns] == [
            column.name for column in expected.columns
        ]
