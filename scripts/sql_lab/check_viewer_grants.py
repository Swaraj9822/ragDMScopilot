"""Drift check for the SQL Lab read-only viewer role's table grants (finding #5).

The viewer role is provisioned with a *broad read, explicit deny* posture
(``GRANT SELECT ON ALL TABLES`` + default privileges, minus a REVOKE of the
Sensitive_Table set — see ``provision_sql_viewer_role.sql``). That posture is a
process control: a newly created sensitive table is readable until someone
remembers to REVOKE it, and an accidental write grant would go unnoticed. This
script turns the two safety invariants into an automated, fail-closed check:

  1. the viewer role holds **only** ``SELECT`` — never ``INSERT``/``UPDATE``/
     ``DELETE``/``TRUNCATE``/``REFERENCES``/``TRIGGER`` on any object;
  2. the viewer role holds **no** privilege on any Sensitive_Table
     (``SQL_LAB_SENSITIVE_TABLES``, e.g. ``users`` / ``refresh_tokens``).

It exits non-zero (printing each violation) on drift, so it can gate a deploy or
run in CI against a provisioned database. It connects **as the viewer role**
(reusing the ``SQL_VIEWER_DB_*`` credentials over the shared ``COPILOT_DB_*``
endpoint) and inspects its own grants, so no admin credentials are required.

Usage:
    python -m scripts.sql_lab.check_viewer_grants
    # or
    python scripts/sql_lab/check_viewer_grants.py
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from rag_system.config import Settings, get_settings

#: The only privilege the read-only viewer role may ever hold.
_ALLOWED_PRIVILEGE = "SELECT"


def find_grant_violations(
    rows: Iterable[tuple[object, object]],
    sensitive_tables: Iterable[str],
) -> list[str]:
    """Return human-readable descriptions of grant drift, or an empty list.

    *rows* is the ``(table_name, privilege_type)`` output of
    ``information_schema.role_table_grants`` for the viewer role. A row is a
    violation when the privilege is anything other than ``SELECT`` (a write/DDL
    grant), or when it is on a Sensitive_Table (which must hold no grant at all,
    not even ``SELECT``). Comparisons are case-insensitive.
    """
    sensitive = {name.strip().lower() for name in sensitive_tables if name and name.strip()}
    violations: list[str] = []
    for table_name, privilege_type in rows:
        table = str(table_name).lower()
        privilege = str(privilege_type).upper()
        if privilege != _ALLOWED_PRIVILEGE:
            violations.append(
                f"non-SELECT privilege {privilege!s} granted on '{table_name}' "
                "(the viewer role must be read-only)"
            )
        if table in sensitive:
            violations.append(
                f"privilege {privilege!s} granted on sensitive table "
                f"'{table_name}' (must be REVOKEd)"
            )
    return violations


def _fetch_viewer_grants(settings: Settings) -> list[tuple[object, object]]:
    """Return the viewer role's ``(table_name, privilege_type)`` grants.

    Connects as the viewer role itself and reads its own grantee rows, so the
    check needs no elevated database credentials.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Install psycopg[binary] to run the grant check.") from exc

    user, password = settings.require_sql_viewer_credentials()
    with psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=user,
        password=password,
        sslmode=settings.copilot_db_sslmode,
    ) as conn:
        return conn.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee = current_user"
        ).fetchall()


def main() -> int:
    settings = get_settings()
    try:
        rows = _fetch_viewer_grants(settings)
    except Exception as exc:  # noqa: BLE001
        # A missing SQL Lab configuration (no viewer credentials) is not drift —
        # SQL Lab simply isn't provisioned here — so skip rather than fail the
        # caller (e.g. a deploy gate). Other errors (connection/query failures)
        # are surfaced as a real failure below.
        from rag_system.sql_lab.errors import SqlLabConfigError

        if isinstance(exc, SqlLabConfigError):
            print("SQL Lab viewer credentials not configured; skipping grant check.")
            return 0
        print(f"SQL viewer grant check could not run: {exc}", file=sys.stderr)
        return 1

    violations = find_grant_violations(rows, settings.sql_lab_sensitive_tables_set)
    if violations:
        print("SQL viewer role grant drift detected:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        print(
            "\nFix: add a REVOKE for the offending object to "
            "scripts/sql_lab/provision_sql_viewer_role.sql (and, for a sensitive "
            "table, to SQL_LAB_SENSITIVE_TABLES), then re-run provisioning.",
            file=sys.stderr,
        )
        return 1
    print(
        f"SQL viewer role grants OK: {len(rows)} grant(s), SELECT-only, "
        "no sensitive-table access."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
