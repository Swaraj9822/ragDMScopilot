"""Provision the SQL Lab read-only ``sql_viewer`` Postgres role via psycopg.

This is a Python port of ``scripts/sql_lab/provision_sql_viewer_role.sql`` for
environments without ``psql`` (e.g. Windows). It performs the same idempotent
steps against the operational database defined by the ``COPILOT_DB_*`` settings:

  1. CREATE/ALTER a minimal LOGIN role (NOSUPERUSER/NOCREATEDB/NOCREATEROLE).
  2. Strip all privileges (fail-closed).
  3. GRANT USAGE on schema public + SELECT on all current tables/views.
  4. ALTER DEFAULT PRIVILEGES so future app-owner tables are auto-granted SELECT.
  5. REVOKE the Sensitive_Table set (users, refresh_tokens).

Connects as the admin/app-owner role (COPILOT_DB_USER), which on Neon owns the
application tables and can CREATE ROLE.

Usage:
    # password taken from SQL_VIEWER_DB_PASSWORD, or generated + printed if unset
    python scripts/sql_lab/provision_sql_viewer_role.py [viewer_role]
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT / "src"))

SENSITIVE_TABLES = ("users", "refresh_tokens")


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    load_env(ENV_PATH)

    import psycopg
    from psycopg import sql

    from rag_system.config import get_settings

    settings = get_settings()

    viewer_role = sys.argv[1] if len(sys.argv) > 1 else "sql_viewer"
    viewer_password = os.environ.get("SQL_VIEWER_DB_PASSWORD") or secrets.token_urlsafe(24)
    generated = "SQL_VIEWER_DB_PASSWORD" not in os.environ
    app_owner = settings.copilot_db_user  # role that owns/creates app tables

    role = sql.Identifier(viewer_role)
    owner = sql.Identifier(app_owner)
    pw = sql.Literal(viewer_password)

    with psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=settings.copilot_db_user,
        password=settings.copilot_db_password,
        sslmode=settings.copilot_db_sslmode,
    ) as conn:
        with conn.cursor() as cur:
            # 1. Create or re-assert the minimal LOGIN role.
            cur.execute(
                sql.SQL("SELECT 1 FROM pg_roles WHERE rolname = {}").format(
                    sql.Literal(viewer_role)
                )
            )
            exists = cur.fetchone() is not None
            verb = sql.SQL("ALTER" if exists else "CREATE")
            cur.execute(
                sql.SQL(
                    "{verb} ROLE {role} LOGIN PASSWORD {pw} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(verb=verb, role=role, pw=pw)
            )

            # 2. Strip everything first (fail-closed).
            for obj in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                cur.execute(
                    sql.SQL(
                        "REVOKE ALL ON ALL {obj} IN SCHEMA public FROM {role}"
                    ).format(obj=sql.SQL(obj), role=role)
                )
            cur.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA public FROM {role}").format(role=role)
            )

            # 3. Name resolution + broad read on current tables/views.
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=role)
            )
            cur.execute(
                sql.SQL(
                    "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}"
                ).format(role=role)
            )

            # 4. Future tables created by the app owner are auto-granted SELECT.
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO {role}"
                ).format(owner=owner, role=role)
            )

            # 5. Subtract the Sensitive_Table set from the broad grant.
            for table in SENSITIVE_TABLES:
                cur.execute(
                    sql.SQL("REVOKE ALL ON {tbl} FROM {role}").format(
                        tbl=sql.Identifier(table), role=role
                    )
                )

            # Verification (informational).
            cur.execute(
                sql.SQL(
                    "SELECT table_name, privilege_type "
                    "FROM information_schema.role_table_grants "
                    "WHERE grantee = {role} ORDER BY table_name, privilege_type"
                ).format(role=sql.Literal(viewer_role))
            )
            grants = cur.fetchall()
        conn.commit()

    print(f"OK: provisioned read-only role '{viewer_role}' (app_owner={app_owner})")
    print(f"    tables readable: {len(grants)}")
    if generated:
        print("\n    Generated viewer password (store securely; set as SQL_VIEWER_DB_PASSWORD):")
        print(f"    SQL_VIEWER_DB_USER={viewer_role}")
        print(f"    SQL_VIEWER_DB_PASSWORD={viewer_password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
