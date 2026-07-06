"""Integration test for the SQL Lab schema listing against a live Postgres.

This test exercises the ``GET /sql/schema`` data path end-to-end at the
database/provisioning level: after provisioning the dedicated read-only
``SQL_Viewer_Role`` with the checked-in
``scripts/sql_lab/provision_sql_viewer_role.sql`` script, it calls
:meth:`rag_system.sql_lab.schema_lister.SqlLabSchemaLister.list_schema` — the
same code that powers the ``GET /sql/schema`` endpoint — connecting as the
viewer role and asserts:

- every approved table (``traces``, ``spans``, ``log_records``) appears in the
  listing, each with a non-empty column list (R7.1);
- no Sensitive_Table (``users``, ``refresh_tokens``) ever appears, because the
  viewer role holds no ``SELECT`` grant on it and the listing is restricted to
  granted objects — the database, not application filtering, is the boundary
  (R7.1, R7.3).

Gating / skipping
-----------------
Mirrors ``tests/test_sql_lab_role_scoping_integration.py``. The whole module is
skipped unless a live database and provisioning prerequisites are available:

- ``COPILOT_DB_HOST`` / ``COPILOT_DB_NAME`` (plus the rest of the
  ``COPILOT_DB_*`` endpoint) — a live Postgres is available;
- ``SQL_VIEWER_DB_USER`` / ``SQL_VIEWER_DB_PASSWORD`` — the viewer credentials to
  provision and connect as;
- admin credentials able to ``CREATE ROLE`` and ``GRANT`` on the approved
  tables, from ``SQL_LAB_ADMIN_DB_USER`` / ``SQL_LAB_ADMIN_DB_PASSWORD`` when
  set, otherwise ``COPILOT_DB_USER`` / ``COPILOT_DB_PASSWORD``;
- the ``psql`` client on ``PATH`` — the role is provisioned by running the
  checked-in provisioning script verbatim.

When any prerequisite is missing the whole module is skipped so the suite still
passes in environments without a database.

Validates:
- R7.1: schema listing returns granted tables + columns over the viewer role
- R7.3: sensitive tables (users/refresh_tokens) never appear in the listing
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

# --- Gating -----------------------------------------------------------------

_PROVISION_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "sql_lab"
    / "provision_sql_viewer_role.sql"
)

_APPROVED_TABLES = ("traces", "spans", "log_records")
_SENSITIVE_TABLES = ("users", "refresh_tokens")


def _admin_user() -> str | None:
    return os.environ.get("SQL_LAB_ADMIN_DB_USER") or os.environ.get("COPILOT_DB_USER")


def _admin_password() -> str | None:
    return os.environ.get("SQL_LAB_ADMIN_DB_PASSWORD") or os.environ.get(
        "COPILOT_DB_PASSWORD"
    )


_MISSING: list[str] = []
if not os.environ.get("COPILOT_DB_HOST"):
    _MISSING.append("COPILOT_DB_HOST")
if not os.environ.get("COPILOT_DB_NAME"):
    _MISSING.append("COPILOT_DB_NAME")
if not (os.environ.get("SQL_VIEWER_DB_USER") and os.environ.get("SQL_VIEWER_DB_PASSWORD")):
    _MISSING.append("SQL_VIEWER_DB_USER/SQL_VIEWER_DB_PASSWORD")
if not (_admin_user() and _admin_password()):
    _MISSING.append("SQL_LAB_ADMIN_DB_USER/PASSWORD (or COPILOT_DB_USER/PASSWORD)")
if shutil.which("psql") is None:
    _MISSING.append("psql client on PATH")
if not _PROVISION_SCRIPT.is_file():
    _MISSING.append(f"provisioning script at {_PROVISION_SCRIPT}")

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason="SQL Lab schema-endpoint integration test requires: " + ", ".join(_MISSING),
)


# --- Helpers ----------------------------------------------------------------


def _settings():
    from rag_system.config import Settings

    return Settings()


def _admin_conn(settings, *, autocommit: bool = True):
    """Open a connection with admin credentials (owner / superuser)."""
    import psycopg

    return psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=_admin_user(),
        password=_admin_password(),
        sslmode=settings.copilot_db_sslmode,
        autocommit=autocommit,
    )


def _ensure_tables_with_rows(settings) -> None:
    """Create the approved + sensitive tables and seed at least one row each.

    Uses the observability + auth schema DDL (idempotent) so the tables the
    provisioning script grants/revokes on exist, then seeds a sample row into
    every approved table.
    """
    from rag_system.auth.schema import SCHEMA_DDL as AUTH_DDL
    from rag_system.observability_tracing.schema import SCHEMA_DDL as OBS_DDL

    trace_id = uuid.uuid4().hex
    with _admin_conn(settings, autocommit=False) as conn:
        with conn.cursor() as cur:
            for statement in (*OBS_DDL, *AUTH_DDL):
                cur.execute(statement)
            cur.execute(
                """
                INSERT INTO traces (trace_id, route, start_ts, duration_ms, root_status)
                VALUES (%s, '/sql-lab-schema-itest', now(), 1, 'success')
                ON CONFLICT (trace_id) DO NOTHING
                """,
                (trace_id,),
            )
            cur.execute(
                """
                INSERT INTO spans
                    (trace_id, span_id, parent_span_id, operation, start_ts,
                     duration_ms, status, attributes)
                VALUES (%s, %s, NULL, 'itest.root', now(), 1, 'success', '{}'::jsonb)
                ON CONFLICT (trace_id, span_id) DO NOTHING
                """,
                (trace_id, uuid.uuid4().hex),
            )
            cur.execute(
                """
                INSERT INTO log_records (ts, level, logger, message, trace_id)
                VALUES (now(), 'INFO', 'itest.logger', 'sql-lab schema itest', %s)
                """,
                (trace_id,),
            )
        conn.commit()


def _run_provisioning_script(settings) -> None:
    """Provision the viewer role by running the checked-in SQL script via psql."""
    viewer_role = os.environ["SQL_VIEWER_DB_USER"]
    viewer_password = os.environ["SQL_VIEWER_DB_PASSWORD"]

    conninfo = (
        f"host={settings.copilot_db_host} "
        f"port={settings.copilot_db_port} "
        f"dbname={settings.copilot_db_name} "
        f"sslmode={settings.copilot_db_sslmode} "
        f"user={_admin_user()}"
    )
    cmd = [
        "psql",
        conninfo,
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"viewer_role={viewer_role}",
        "-v",
        f"viewer_password={viewer_password}",
        "-f",
        str(_PROVISION_SCRIPT),
    ]
    env = dict(os.environ)
    env["PGPASSWORD"] = _admin_password() or ""
    completed = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(
            "Provisioning script failed (exit "
            f"{completed.returncode}).\nstdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


@pytest.fixture(scope="module")
def provisioned():
    """Seed tables and provision the viewer role once for the module."""
    settings = _settings()
    _ensure_tables_with_rows(settings)
    _run_provisioning_script(settings)
    return settings


# --- Test -------------------------------------------------------------------


class TestSchemaEndpointListing:
    """Validates: R7.1, R7.3 — granted tables listed, sensitive tables absent."""

    def test_list_schema_returns_only_granted_tables(self, provisioned):
        from rag_system.sql_lab.schema_lister import SqlLabSchemaLister

        settings = provisioned
        tables = SqlLabSchemaLister(settings).list_schema()

        listed = {table.name: table for table in tables}

        # Every approved table appears with a non-empty column list (R7.1).
        for name in _APPROVED_TABLES:
            assert name in listed, (
                f"expected approved table {name!r} in schema listing (R7.1); "
                f"got {sorted(listed)}"
            )
            assert listed[name].columns, (
                f"expected columns for approved table {name!r} (R7.1)"
            )
            for column in listed[name].columns:
                assert column.name, "each column must expose a name (R7.1)"
                assert column.type, "each column must expose a type (R7.1)"

        # No Sensitive_Table ever appears — the viewer role holds no SELECT grant
        # on it, so the grant-restricted listing excludes it entirely (R7.3).
        for name in _SENSITIVE_TABLES:
            assert name not in listed, (
                f"sensitive table {name!r} must never appear in the schema "
                f"listing (R7.3); got {sorted(listed)}"
            )
