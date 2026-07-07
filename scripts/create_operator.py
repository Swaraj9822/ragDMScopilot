"""Create (or elevate) an operator account directly in the users table.

Self-service registration is closed (RAG_AUTH_ALLOW_REGISTRATION=false) and the
RAG_OPERATOR_EMAILS allow-list is empty, so an operator must be inserted with
its stored ``is_operator`` flag set to TRUE. This script does that idempotently:
if the (case-insensitive) email already exists it resets the password and grants
operator; otherwise it inserts a new active operator account.

Usage:
    python scripts/create_operator.py <email> <password>
    python scripts/create_operator.py            # uses the defaults below

Passwords are hashed with the same rag_system.auth.passwords.hash_password used
by the running app, so the account logs in normally.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_EMAIL = "admin@gmail.com"
DEFAULT_PASSWORD = "admin123"


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    email = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EMAIL).strip()
    password = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PASSWORD

    load_env(ENV_PATH)

    from rag_system.auth import schema
    from rag_system.auth.passwords import hash_password
    from rag_system.config import get_settings

    settings = get_settings()
    password_hash = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()

    # Ensure the users table exists before touching it.
    schema.apply_schema(settings)

    with schema.connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, is_operator FROM users WHERE lower(email) = lower(%s)",
                (email,),
            )
            existing = cur.fetchone()

            if existing is not None:
                user_id = str(existing[0])
                cur.execute(
                    """
                    UPDATE users
                       SET password_hash = %s,
                           is_operator   = TRUE,
                           is_active     = TRUE
                     WHERE id = %s
                    """,
                    (password_hash, user_id),
                )
                action = "updated existing account -> operator"
            else:
                user_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO users
                        (id, email, password_hash, is_active, is_operator, created_at)
                    VALUES (%s, %s, %s, TRUE, TRUE, %s::timestamptz)
                    """,
                    (user_id, email, password_hash, now),
                )
                action = "created new operator account"
        conn.commit()

    print(f"OK: {action}")
    print(f"    email    = {email}")
    print(f"    id       = {user_id}")
    print("    operator = True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
