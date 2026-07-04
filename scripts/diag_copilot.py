"""Diagnostic: inspect product names + sales row counts for the copilot query."""

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
        for tbl in ("sales_invoice_item", "sales_order_item", "sales_invoice", "sales_order"):
            cur.execute(f"SELECT count(*) FROM {tbl}")
            print(f"{tbl:<20} rows = {cur.fetchone()[0]}")
        print()

        for tbl in ("sales_invoice_item", "sales_order_item"):
            print(f"=== {tbl}: product_name ILIKE '%life%' OR '%cheese%' ===")
            cur.execute(
                f"""
                SELECT product_name, count(*) AS n, sum(qty) AS total_qty
                FROM {tbl}
                WHERE product_name ILIKE '%life%' OR product_name ILIKE '%cheese%'
                GROUP BY product_name
                ORDER BY n DESC
                LIMIT 30
                """
            )
            rows = cur.fetchall()
            if not rows:
                print("  (none)")
            for pn, n, q in rows:
                print(f"  {pn!r:<45} n={n} total_qty={q}")
            print()

        print("=== sample product_name values (sales_invoice_item) ===")
        cur.execute(
            "SELECT DISTINCT product_name FROM sales_invoice_item ORDER BY product_name LIMIT 40"
        )
        for (pn,) in cur.fetchall():
            print(f"  {pn!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
