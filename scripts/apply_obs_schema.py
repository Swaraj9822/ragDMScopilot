"""Apply the observability schema to the copilot DB, then verify.

Uses the same DDL the app runs on startup (rag_system.observability_tracing.schema)
so the result is identical to an app-driven migration. Idempotent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT / "src"))


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    load_env(ENV_PATH)

    from rag_system.config import get_settings
    from rag_system.observability_tracing import schema

    settings = get_settings()
    print(f"Applying observability schema to db='{settings.copilot_db_name}' "
          f"host='{settings.copilot_db_host}' ...")
    schema.apply_schema(settings)
    print("apply_schema completed.")

    # Verify
    import psycopg

    with psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=settings.copilot_db_user,
        password=settings.copilot_db_password,
        sslmode=settings.copilot_db_sslmode,
        connect_timeout=15,
    ) as conn:
        with conn.cursor() as cur:
            for t in ("traces", "spans", "log_records"):
                cur.execute(
                    "SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",)
                )
                exists = cur.fetchone()[0]
                print(f"  [{'OK ' if exists else 'MISSING'}] {t}")
            # index sanity check
            cur.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE schemaname='public'
                  AND tablename IN ('traces','spans','log_records')
                ORDER BY indexname
                """
            )
            idx = [r[0] for r in cur.fetchall()]
            print(f"  indexes ({len(idx)}): {', '.join(idx)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
