"""Standalone RDS PostgreSQL connection smoke test.

Run from the project root:
    python rdscon.py

The script reads `.env` if present and uses either DATABASE_URL or the
COPILOT_DB_* variables already used by this project.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without requiring python-dotenv."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def build_connection_info() -> str | dict[str, str | int]:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    required_keys = [
        "COPILOT_DB_HOST",
        "COPILOT_DB_PORT",
        "COPILOT_DB_NAME",
        "COPILOT_DB_USER",
        "COPILOT_DB_PASSWORD",
    ]
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return {
        "host": os.environ["COPILOT_DB_HOST"],
        "port": int(os.environ["COPILOT_DB_PORT"]),
        "dbname": os.environ["COPILOT_DB_NAME"],
        "user": os.environ["COPILOT_DB_USER"],
        "password": os.environ["COPILOT_DB_PASSWORD"],
        "sslmode": os.getenv("COPILOT_DB_SSLMODE", "require"),
        "connect_timeout": int(os.getenv("COPILOT_DB_CONNECT_TIMEOUT", "10")),
    }


def describe_target(connection_info: str | dict[str, str | int]) -> str:
    if isinstance(connection_info, str):
        return "DATABASE_URL"

    return (
        f"{connection_info['user']}@{connection_info['host']}:"
        f"{connection_info['port']}/{connection_info['dbname']}"
    )


def main() -> int:
    load_dotenv(ENV_FILE)

    try:
        connection_info = build_connection_info()
        target = describe_target(connection_info)
        print(f"Testing RDS PostgreSQL connection to {target} ...")

        connect_kwargs = {}
        if isinstance(connection_info, dict):
            connect_kwargs = connection_info
            connection_info = ""

        with psycopg.connect(connection_info, **connect_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                result = cur.fetchone()

        if result == (1,):
            print("Connection successful.")
            return 0

        print(f"Connection opened, but health query returned unexpected result: {result}")
        return 1
    except Exception as exc:
        print("Connection failed.")
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
