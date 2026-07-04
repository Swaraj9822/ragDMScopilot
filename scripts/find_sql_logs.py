"""Pull log records for the copilot 'life cheese' trace to find the generated SQL."""

from __future__ import annotations

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
        cur.execute(
            """
            SELECT ts, level, logger, message, extra
            FROM log_records
            WHERE message ILIKE '%select%'
               OR message ILIKE '%cheese%'
               OR message ILIKE '%sql%'
               OR logger ILIKE '%copilot%'
            ORDER BY ts DESC
            LIMIT 25
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("No matching log records.")
        for ts, level, logger, message, extra in rows:
            print(f"[{ts}] {level} {logger}")
            print(f"   {message}")
            if extra and str(extra) not in ("{}", "None"):
                print(f"   extra: {extra}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
