"""Integration tests for SQL Lab SQL_Viewer_Role scoping against a live Postgres.

These tests exercise the **primary security boundary** for SQL Lab: the
dedicated read-only Postgres role provisioned by
``scripts/sql_lab/provision_sql_viewer_role.sql`` (documented in
``docs/sql-lab/provisioning.md``). They verify, at the database/provisioning
level (not the application guard), that once the role is provisioned:

- reading a Sensitive_Table (``users`` / ``refresh_tokens``) is denied with an
  authorization error and yields zero rows (R2.1, R2.2);
- the role holds ``SELECT`` only on the approved tables and none on the
  sensitive tables (R2.3);
- any write / DDL statement is rejected by the database and leaves state
  unchanged (R2.5);
- reading an approved table for which the role holds a ``SELECT`` grant returns
  rows without an authorization error (R2.6).

Gating / skipping
-----------------
Like ``tests/test_integration_postgres.py`` these tests require a live database
and are skipped automatically otherwise. They additionally require:

- ``COPILOT_DB_HOST`` (plus the rest of the ``COPILOT_DB_*`` endpoint) — a live
  Postgres is available;
- ``SQL_VIEWER_DB_USER`` / ``SQL_VIEWER_DB_PASSWORD`` — the viewer credentials to
  provision and connect as;
- admin credentials able to ``CREATE ROLE`` and ``GRANT`` on the approved
  tables. These are taken from ``SQL_LAB_ADMIN_DB_USER`` /
  ``SQL_LAB_ADMIN_DB_PASSWORD`` when set, otherwise they fall back to
  ``COPILOT_DB_USER`` / ``COPILOT_DB_PASSWORD`` (which must then own the tables
  and hold ``CREATEROLE``, e.g. be a superuser);
- the ``psql`` client on ``PATH`` — the role is provisioned by running the
  checked-in provisioning script verbatim, exactly as an operator would.

When any prerequisite is missing the whole module is skipped so the suite still
passes in environments without a database.

Validates:
- R2.1: ``users`` denied (authorization error, zero rows) as the viewer role
- R2.2: ``refresh_tokens`` denied (authorization error, zero rows)
- R2.3: viewer role holds SELECT only on approved tables, none on sensitive
- R2.5: writes/DDL rejected at the db level; db state unchanged
- R2.6: approved tables return rows without an authorization error
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
    reason="SQL Lab role-scoping integration test requires: " + ", ".join(_MISSING),
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


def _viewer_conn(settings, *, autocommit: bool = True):
    """Open a connection as the provisioned SQL_Viewer_Role."""
    import psycopg
    from psycopg.rows import dict_row

    user, password = settings.require_sql_viewer_credentials()
    return psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=user,
        password=password,
        sslmode=settings.copilot_db_sslmode,
        autocommit=autocommit,
        row_factory=dict_row,
    )


def _ensure_tables_with_rows(settings) -> None:
    """Create the approved + sensitive tables and seed at least one row each.

    Uses the observability + auth schema DDL (idempotent) so the tables the
    provisioning script grants/revokes on exist, then seeds a sample row into
    every approved table so R2.6 has data to return.
    """
    from rag_system.auth.schema import SCHEMA_DDL as AUTH_DDL
    from rag_system.observability_tracing.schema import SCHEMA_DDL as OBS_DDL

    trace_id = uuid.uuid4().hex
    with _admin_conn(settings, autocommit=False) as conn:
        with conn.cursor() as cur:
            for statement in (*OBS_DDL, *AUTH_DDL):
                cur.execute(statement)
            # Seed one trace, one span, one log record (approved tables).
            cur.execute(
                """
                INSERT INTO traces (trace_id, route, start_ts, duration_ms, root_status)
                VALUES (%s, '/sql-lab-itest', now(), 1, 'success')
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
                VALUES (now(), 'INFO', 'itest.logger', 'sql-lab role scoping itest', %s)
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


# --- Tests ------------------------------------------------------------------


class TestSensitiveTableAccessDenied:
    """Validates: R2.1, R2.2 — sensitive tables denied (authz error, zero rows)."""

    @pytest.mark.parametrize("table", _SENSITIVE_TABLES)
    def test_sensitive_table_select_is_denied_with_zero_rows(self, provisioned, table):
        import psycopg

        settings = provisioned
        with _viewer_conn(settings) as conn:
            fetched_rows = None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur = conn.execute(f"SELECT * FROM {table}")
                # Should not reach here; if it does, prove zero rows are exposed.
                fetched_rows = cur.fetchall()

            # The read never yielded any rows from the sensitive table (R2.1/R2.2).
            assert fetched_rows is None or fetched_rows == []


class TestApprovedTableGrantsOnly:
    """Validates: R2.3 — SELECT granted only on approved tables, none on sensitive."""

    def test_role_grants_match_approved_table_list(self, provisioned):
        settings = provisioned
        viewer_role = os.environ["SQL_VIEWER_DB_USER"]
        with _admin_conn(settings) as conn:
            rows = conn.execute(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE grantee = %s
                ORDER BY table_name, privilege_type
                """,
                (viewer_role,),
            ).fetchall()

        granted = {(name, priv) for name, priv in rows}

        # Every approved table has exactly a SELECT grant.
        for table in _APPROVED_TABLES:
            assert (table, "SELECT") in granted, (
                f"expected SELECT grant on approved table {table!r} (R2.3)"
            )

        # No privilege of any kind on the sensitive tables.
        sensitive_grants = {
            (name, priv) for (name, priv) in granted if name in _SENSITIVE_TABLES
        }
        assert sensitive_grants == set(), (
            f"viewer role must hold no privilege on {_SENSITIVE_TABLES} (R2.3), "
            f"found: {sensitive_grants}"
        )

        # No write/DDL privileges anywhere — SELECT is the only privilege type.
        non_select = {(name, priv) for (name, priv) in granted if priv != "SELECT"}
        assert non_select == set(), (
            f"viewer role must hold only SELECT privileges (R2.3), found: {non_select}"
        )


class TestWritesAndDdlRejected:
    """Validates: R2.5 — writes/DDL rejected at db level; db state unchanged."""

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO traces (trace_id, route, start_ts, duration_ms, root_status) "
            "VALUES ('itest-write', '/x', now(), 1, 'success')",
            "UPDATE traces SET route = '/hacked'",
            "DELETE FROM traces",
            "CREATE TABLE sql_lab_itest_tmp (id int)",
            "DROP TABLE traces",
            "ALTER TABLE traces ADD COLUMN hacked int",
        ],
    )
    def test_write_or_ddl_is_rejected(self, provisioned, statement):
        import psycopg

        settings = provisioned

        # Snapshot approved-table state as admin before the attempt.
        with _admin_conn(settings) as admin:
            before = admin.execute("SELECT count(*) AS c FROM traces").fetchone()[0]

        with _viewer_conn(settings) as conn:
            with pytest.raises(psycopg.errors.Error):
                conn.execute(statement)

        # Database state is unchanged after the rejected statement (R2.5).
        with _admin_conn(settings) as admin:
            after = admin.execute("SELECT count(*) AS c FROM traces").fetchone()[0]
            # The traces table still exists (DROP/ALTER were rejected) and its
            # row count is unchanged (INSERT/UPDATE/DELETE were rejected).
            assert after == before


class TestApprovedTableReadsSucceed:
    """Validates: R2.6 — approved tables return rows without an authz error."""

    @pytest.mark.parametrize("table", _APPROVED_TABLES)
    def test_approved_table_returns_rows(self, provisioned, table):
        settings = provisioned
        with _viewer_conn(settings) as conn:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()

        # Seeded data means each approved table returns at least one row, and no
        # authorization error was raised reaching this assertion (R2.6).
        assert len(rows) >= 1
