"""Confirm exact-match returns 0 but ILIKE partial-match returns the real data."""

from __future__ import annotations

from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
env = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

conn = psycopg.connect(
    host=env["COPILOT_DB_HOST"], port=int(env["COPILOT_DB_PORT"]),
    dbname=env["COPILOT_DB_NAME"], user=env["COPILOT_DB_USER"],
    password=env["COPILOT_DB_PASSWORD"], sslmode="require", connect_timeout=15,
)
with conn, conn.cursor() as cur:
    cases = [
        ("exact  product_name = 'life cheese'", "product_name = 'life cheese'"),
        ("exact  lower(pn) = lower('Life Cheese')", "LOWER(product_name) = LOWER('Life Cheese')"),
        ("ILIKE  '%life cheese%'", "product_name ILIKE '%life cheese%'"),
    ]
    for label, where in cases:
        cur.execute(f"SELECT COALESCE(SUM(qty),0) FROM sales_invoice_item WHERE {where}")
        print(f"{label:<45} total_qty = {cur.fetchone()[0]}")
