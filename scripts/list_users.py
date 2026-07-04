"""List login accounts in the users table (email, active/operator flags)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    import psycopg

    conn = psycopg.connect(
        host=env["COPILOT_DB_HOST"],
        port=int(env.get("COPILOT_DB_PORT", "5432")),
        dbname=env["COPILOT_DB_NAME"],
        user=env["COPILOT_DB_USER"],
        password=env["COPILOT_DB_PASSWORD"],
        sslmode=env.get("COPILOT_DB_SSLMODE", "require"),
        connect_timeout=15,
    )
    with conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.users') IS NOT NULL")
        if not cur.fetchone()[0]:
            print("users table does not exist yet.")
            return 0
        cur.execute(
            """
            SELECT email, is_active, is_operator, created_at
            FROM users
            ORDER BY created_at
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("No users found (0 accounts). Registration may be needed.")
            return 0
        print(f"{len(rows)} user account(s):\n")
        print(f"{'EMAIL':<40} {'ACTIVE':<7} {'OPERATOR':<9} CREATED")
        print("-" * 80)
        for email, active, operator, created in rows:
            print(f"{email:<40} {str(active):<7} {str(operator):<9} {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
