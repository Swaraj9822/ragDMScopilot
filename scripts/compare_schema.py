"""Compare the live Neon/Postgres schema against the copilot schema catalog.

Reads COPILOT_DB_* connection details from .env, introspects information_schema,
and diffs the live database against config/copilot_schema_catalog.json.
Reports missing tables, missing columns, and type mismatches.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "config" / "copilot_schema_catalog.json"
ENV_PATH = ROOT / ".env"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def main() -> int:
    env = load_env(ENV_PATH)
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    expected: dict[str, dict[str, str]] = {}
    for tbl in catalog.get("tables", []):
        cols = {c["name"]: c["type"] for c in tbl.get("columns", [])}
        expected[tbl["name"]] = cols

    try:
        import psycopg
    except ImportError:
        print("psycopg not installed; run: pip install 'psycopg[binary]'", file=sys.stderr)
        return 2

    conn = psycopg.connect(
        host=env["COPILOT_DB_HOST"],
        port=int(env.get("COPILOT_DB_PORT", "5432")),
        dbname=env["COPILOT_DB_NAME"],
        user=env["COPILOT_DB_USER"],
        password=env["COPILOT_DB_PASSWORD"],
        sslmode=env.get("COPILOT_DB_SSLMODE", "require"),
        connect_timeout=15,
    )

    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version()")
            dbname, dbuser, version = cur.fetchone()
            print(f"Connected to database='{dbname}' user='{dbuser}'")
            print(f"Server: {version.split(',')[0]}")
            print()

            # Live tables in public schema
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            live_tables = [r[0] for r in cur.fetchall()]

            # Live columns per table
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
            live_cols: dict[str, dict[str, str]] = {}
            for tname, cname, dtype in cur.fetchall():
                live_cols.setdefault(tname, {})[cname] = dtype

    print(f"Live public tables ({len(live_tables)}): {', '.join(live_tables) or '(none)'}")
    print()

    # Observability tables check
    obs_required = ["traces", "spans", "log_records"]
    print("=== Observability tables ===")
    for t in obs_required:
        print(f"  [{'OK ' if t in live_tables else 'MISSING'}] {t}")
    print()

    print("=== Copilot business tables ===")
    missing_tables = []
    for tname, ecols in expected.items():
        if tname not in live_cols:
            missing_tables.append(tname)
            print(f"  [MISSING TABLE] {tname} ({len(ecols)} cols expected)")
            continue
        lcols = live_cols[tname]
        missing_cols = [c for c in ecols if c not in lcols]
        type_mismatch = [
            (c, ecols[c], lcols[c])
            for c in ecols
            if c in lcols and ecols[c] != lcols[c]
        ]
        status = "OK" if not missing_cols and not type_mismatch else "DIFF"
        print(f"  [{status}] {tname} (expected {len(ecols)} cols, live {len(lcols)} cols)")
        for c in missing_cols:
            print(f"        - missing column: {c} ({ecols[c]})")
        for c, et, lt in type_mismatch:
            print(f"        ~ type mismatch: {c} expected '{et}' but live '{lt}'")
    print()

    print("=== Summary ===")
    print(f"  Expected copilot tables : {len(expected)}")
    print(f"  Present in live DB       : {len(expected) - len(missing_tables)}")
    print(f"  Missing copilot tables   : {len(missing_tables)} -> {missing_tables or 'none'}")
    obs_missing = [t for t in obs_required if t not in live_tables]
    print(f"  Missing observability    : {obs_missing or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
