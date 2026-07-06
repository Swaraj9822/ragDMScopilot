"""SQL Lab read-only query executor (viewer-role connection).

:class:`SqlLabExecutor` runs an already-guard-approved statement using the
**exact** transaction pattern of :class:`rag_system.copilot.PostgresCopilotExecutor`
(``SET TRANSACTION READ ONLY`` → transaction-local ``statement_timeout`` via
``set_config(..., true)`` → ``fetchmany`` → ``rollback``), so its runtime
behavior is identical to the copilot executor. It differs only in

* **credentials** — it authenticates with the dedicated read-only
  ``SQL_VIEWER_DB_USER``/``SQL_VIEWER_DB_PASSWORD`` role rather than the copilot
  role, while reusing the shared ``COPILOT_DB_HOST/PORT/NAME/SSLMODE`` endpoint
  (R1.3), and
* **limits** — it fetches ``sql_lab_row_limit + 1`` rows (the extra row lets the
  service detect truncation without a second round-trip) and applies
  ``sql_lab_statement_timeout_ms``.

Failure modes map onto the SQL Lab error hierarchy: missing credentials →
:class:`SqlLabConfigError` (keyed, value-free, R1.5), connection failure →
:class:`SqlLabConnectionError` (R1.6), statement timeout →
:class:`SqlLabTimeoutError` (R4.9), and any other database error →
:class:`SqlLabExecutionError` carrying the database message (R4.13). The
dedicated read-only role remains the *primary* security boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rag_system.config import Settings
from rag_system.sql_lab.errors import (
    SqlLabConnectionError,
    SqlLabExecutionError,
    SqlLabTimeoutError,
)


@dataclass(frozen=True)
class ExecutionResult:
    """Raw execution outcome handed back to the service for Result_Set shaping.

    ``rows`` may contain up to ``row_limit + 1`` entries; the service trims to
    ``row_limit`` and derives ``truncated`` from the overflow. ``columns`` is
    the select-order column list, ``row_count`` is ``len(rows)`` as fetched, and
    ``duration_ms`` is the whole-millisecond execution time measured with
    :func:`time.perf_counter`. ``truncated`` reports whether more than
    ``row_limit`` rows were produced (i.e. the extra probe row was fetched).
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    duration_ms: int
    truncated: bool


class SqlLabExecutor:
    """Execute an approved read-only ``SELECT`` over the viewer-role connection."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def execute(self, sql: str) -> ExecutionResult:
        """Run an approved statement using the copilot transaction pattern.

        Raises :class:`SqlLabConfigError` when a viewer credential is missing,
        :class:`SqlLabConnectionError` when the connection cannot be
        established, :class:`SqlLabTimeoutError` when the statement timeout is
        exceeded, and :class:`SqlLabExecutionError` for any other database
        error (carrying the database message).
        """
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError("Install psycopg[binary] to use SQL Lab.") from exc

        # Keyed, value-free error if a viewer credential is absent (R1.5).
        user, password = self._settings.require_sql_viewer_credentials()

        row_limit = self._settings.sql_lab_row_limit
        timeout_ms = self._settings.sql_lab_statement_timeout_ms

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
            # Connection could not be established with the viewer credentials.
            # Never surface the credential values (R1.6).
            raise SqlLabConnectionError(
                "Failed to connect to the SQL Lab viewer database."
            ) from exc

        try:
            # --- Transaction body mirrored from PostgresCopilotExecutor ---
            # psycopg connects with autocommit=False, so the first execute()
            # implicitly opens a transaction *before* the statement runs. A bare
            # "BEGIN READ ONLY" would therefore execute inside that already-open
            # transaction — Postgres warns "there is already a transaction in
            # progress" and the READ ONLY attribute is never applied. Use
            # "SET TRANSACTION READ ONLY", which is valid inside the open
            # transaction and applies to it, as the guard against writes.
            #
            # NOTE: we deliberately do NOT set default_transaction_read_only via
            # the connection ``options`` startup parameter — pooled providers
            # (e.g. Neon's PgBouncer pooler) reject unknown startup parameters
            # ("unsupported startup parameter in options"). SET TRANSACTION is a
            # plain SQL command and works on both pooled and direct connections.
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(timeout_ms),),
            )
            start = time.perf_counter()
            try:
                cur = conn.execute(sql)
                # Fetch one extra row so the service can detect truncation
                # without a second COUNT(*) round-trip.
                fetched = cur.fetchmany(row_limit + 1)
            except psycopg.errors.QueryCanceled as exc:
                # statement_timeout tripped: roll back and surface a timeout.
                conn.rollback()
                raise SqlLabTimeoutError(
                    f"Query exceeded the statement timeout of {timeout_ms} ms."
                ) from exc
            except psycopg.Error as exc:
                # Any other database error: roll back and carry the db message.
                conn.rollback()
                raise SqlLabExecutionError(str(exc)) from exc
            duration_ms = int((time.perf_counter() - start) * 1000)
            conn.rollback()

            rows = [dict(row) for row in fetched]
            columns = self._extract_columns(cur, rows)
            truncated = len(rows) > row_limit
            return ExecutionResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                duration_ms=duration_ms,
                truncated=truncated,
            )
        finally:
            conn.close()

    @staticmethod
    def _extract_columns(cur: Any, rows: list[dict[str, Any]]) -> list[str]:
        """Return column names in select order.

        Prefer the cursor description (present even for zero-row results); fall
        back to the keys of the first fetched row.
        """
        description = getattr(cur, "description", None)
        if description:
            return [col.name for col in description]
        if rows:
            return list(rows[0].keys())
        return []


__all__ = ["SqlLabExecutor", "ExecutionResult"]
