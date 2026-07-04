"""Find the most recent copilot-generated SQL from the traces/spans tables."""

from __future__ import annotations

import json
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
        # Search span attributes for anything containing SQL or 'cheese'
        cur.execute(
            """
            SELECT trace_id, operation, start_ts, attributes
            FROM spans
            WHERE attributes::text ILIKE '%cheese%'
               OR attributes::text ILIKE '%select%'
            ORDER BY start_ts DESC
            LIMIT 10
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("No spans with SQL/cheese attributes found.")
        for trace_id, op, ts, attrs in rows:
            print(f"--- {ts}  op={op}  trace={trace_id}")
            try:
                a = attrs if isinstance(attrs, dict) else json.loads(attrs)
            except Exception:
                a = {"raw": str(attrs)}
            for k, v in a.items():
                sv = str(v)
                if len(sv) > 400:
                    sv = sv[:400] + "…"
                print(f"    {k}: {sv}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
