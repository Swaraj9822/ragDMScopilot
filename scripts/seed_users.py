"""Seed login accounts directly into the users table.

Uses the app's own PostgresUserStore + bcrypt hash_password so the accounts
authenticate exactly like registered ones. Idempotent: skips emails that
already exist (case-insensitive).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT / "src"))

# email, password, is_operator
ACCOUNTS = [
    ("swaraj.bagal23@gmail.com", "Swaraj7709", False),
    ("admin@email.com", "admin123", False),
]


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    load_env(ENV_PATH)

    from rag_system.auth.passwords import hash_password
    from rag_system.auth.store import EmailAlreadyExistsError, PostgresUserStore
    from rag_system.config import get_settings

    settings = get_settings()
    store = PostgresUserStore(settings)

    for email, password, _is_operator in ACCOUNTS:
        try:
            record = store.create_user(email, hash_password(password))
            print(f"CREATED  {email}  (active={record.is_active}, operator={record.is_operator})")
        except EmailAlreadyExistsError:
            print(f"SKIPPED  {email}  (already exists)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
