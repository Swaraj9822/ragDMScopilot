"""SQL Lab schema lister (viewer-role ``information_schema`` browse).

:class:`SqlLabSchemaLister` powers the ``GET /sql/schema`` endpoint (Slice 2,
R7.1–R7.4). It connects with the **exact** read-only viewer connection pattern
used by :class:`~rag_system.sql_lab.executor.SqlLabExecutor` — the dedicated
``SQL_VIEWER_DB_USER``/``SQL_VIEWER_DB_PASSWORD`` role over the shared
``COPILOT_DB_HOST/PORT/NAME/SSLMODE`` endpoint, inside a ``SET TRANSACTION READ
ONLY`` transaction that always ends in ``rollback`` — and lists tables and their
columns from ``information_schema``.

The listing is **restricted to objects the viewer role holds a ``SELECT`` grant
on** by joining ``information_schema.columns`` against
``information_schema.role_table_grants`` filtered to the connected role's
``SELECT`` privileges (R7.1). Because the viewer role holds no grant on the
Sensitive_Tables (``users``/``refresh_tokens``), those tables can never appear
in the result (R7.3) — the database, not application filtering, is the boundary.

Failure is all-or-nothing: any missing credential, connection failure, or query
error raises (respectively :class:`SqlLabConfigError`,
:class:`SqlLabConnectionError`, or :class:`SqlLabExecutionError`) and **no
partial table list is ever returned** (R7.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from rag_system.config import Settings
from rag_system.sql_lab.errors import (
    SqlLabConnectionError,
    SqlLabExecutionError,
)

#: List tables + columns visible to the connected (viewer) role, ordered so the
#: service can group columns under their table in a single pass. Restricted to
#: the ``public`` schema and to tables the role holds a ``SELECT`` grant on via
#: ``role_table_grants`` (grantee is the current role), so ungranted sensitive
#: tables never appear (R7.1, R7.3).
_SCHEMA_QUERY = """
SELECT c.table_name, c.column_name, c.data_type
FROM information_schema.columns AS c
WHERE c.table_schema = 'public'
  AND c.table_name IN (
    SELECT g.table_name
    FROM information_schema.role_table_grants AS g
    WHERE g.table_schema = 'public'
      AND g.privilege_type = 'SELECT'
      AND g.grantee = current_user
  )
ORDER BY c.table_name, c.ordinal_position
"""


@dataclass(frozen=True)
class SchemaColumn:
    """A single column exposed in the schema listing."""

    name: str
    type: str


@dataclass(frozen=True)
class SchemaTable:
    """A table and its columns, restricted to viewer-``SELECT``-grantable objects."""

    name: str
    columns: list[SchemaColumn]


class SqlLabSchemaLister:
    """List granted tables + columns over the read-only viewer connection."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def list_schema(self) -> list[SchemaTable]:
        """Return every granted table with its columns, or raise on any failure.

        Raises :class:`SqlLabConfigError` when a viewer credential is missing
        (keyed, value-free), :class:`SqlLabConnectionError` when the connection
        cannot be established, and :class:`SqlLabExecutionError` for any other
        database error. On any failure no partial list is returned (R7.4).
        """
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError("Install psycopg[binary] to use SQL Lab.") from exc

        # Keyed, value-free error if a viewer credential is absent (R1.5).
        user, password = self._settings.require_sql_viewer_credentials()

        # Reuse the shared COPILOT_DB_* endpoint, substitute viewer creds (R1.3).
        try:
            conn = psycopg.connect(
                host=self._settings.copilot_db_host,
                port=self._settings.copilot_db_port,
                dbname=self._settings.copilot_db_name,
                user=user,
                password=password,
                sslmode=self._settings.copilot_db_sslmode,
                row_factory=dict_row,
            )
        except psycopg.OperationalError as exc:
            # Never surface the credential values (R1.6, R7.4).
            raise SqlLabConnectionError(
                "Failed to connect to the SQL Lab viewer database."
            ) from exc

        try:
            # Read-only transaction, mirrored from SqlLabExecutor. SET
            # TRANSACTION (not BEGIN READ ONLY) applies to the already-open
            # transaction and works on pooled providers.
            conn.execute("SET TRANSACTION READ ONLY")
            try:
                cur = conn.execute(_SCHEMA_QUERY)
                fetched = cur.fetchall()
            except psycopg.Error as exc:
                # Roll back and surface the failure; never a partial list (R7.4).
                conn.rollback()
                raise SqlLabExecutionError(str(exc)) from exc
            conn.rollback()
        finally:
            conn.close()

        return self._group_rows(fetched)

    @staticmethod
    def _group_rows(rows: list[dict[str, object]]) -> list[SchemaTable]:
        """Group ordered ``(table_name, column_name, data_type)`` rows by table.

        The query orders by ``table_name`` then ``ordinal_position``, so a
        single pass preserves both table encounter order and in-table column
        order.
        """
        tables: list[SchemaTable] = []
        columns_by_index: dict[str, list[SchemaColumn]] = {}
        for row in rows:
            table_name = str(row["table_name"])
            column = SchemaColumn(
                name=str(row["column_name"]),
                type=str(row["data_type"]),
            )
            existing = columns_by_index.get(table_name)
            if existing is None:
                existing = []
                columns_by_index[table_name] = existing
                tables.append(SchemaTable(name=table_name, columns=existing))
            existing.append(column)
        return tables


__all__ = ["SqlLabSchemaLister", "SchemaTable", "SchemaColumn"]
