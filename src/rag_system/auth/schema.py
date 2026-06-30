"""PostgreSQL schema (DDL) for self-managed authentication.

Defines the ``users`` table that backs registration and login. Connectivity
reuses the existing ``COPILOT_DB_*`` settings, matching the psycopg connection
style of :class:`rag_system.copilot.PostgresCopilotExecutor` and
:mod:`rag_system.observability_tracing.schema`.

The DDL is idempotent (``CREATE TABLE/INDEX IF NOT EXISTS``) so
:func:`apply_schema` can run on every startup without error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rag_system.config import Settings
from rag_system.observability import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

logger = get_logger(__name__)

__all__ = ["USERS_DDL", "REFRESH_TOKENS_DDL", "SCHEMA_DDL", "connect", "create_schema", "apply_schema"]


USERS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            UUID PRIMARY KEY,
        email         TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        is_active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Case-insensitive uniqueness: nobody can register "User@x.com" and
    # "user@x.com" as two accounts, and login can match case-insensitively.
    "CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_key ON users (lower(email))",
)
"""DDL for the ``users`` table and its case-insensitive unique email index."""

REFRESH_TOKENS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS refresh_tokens (
        id          UUID PRIMARY KEY,
        user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash  TEXT NOT NULL UNIQUE,
        issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at  TIMESTAMPTZ NOT NULL,
        revoked_at  TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS refresh_tokens_user_idx ON refresh_tokens (user_id)",
)
"""DDL for the ``refresh_tokens`` table (rotation + revocation support).

Only the SHA-256 hash of each opaque refresh token is stored, so a database
read never exposes a usable token. ``revoked_at`` marks a token rotated or
logged out; a non-expired revoked token presented again signals reuse."""

# Ordered so the FK target (``users``) is created before ``refresh_tokens``.
SCHEMA_DDL: tuple[str, ...] = (*USERS_DDL, *REFRESH_TOKENS_DDL)
"""All auth schema DDL statements in dependency order."""


def connect(settings: Settings) -> Connection[Any]:
    """Open a psycopg connection using the ``COPILOT_DB_*`` settings.

    psycopg is imported lazily so the dependency is only required when a live
    database is used; missing connection settings raise a clear error naming the
    absent values.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Install psycopg[binary] to use authentication."
        ) from exc

    required = {
        "COPILOT_DB_HOST": settings.copilot_db_host,
        "COPILOT_DB_NAME": settings.copilot_db_name,
        "COPILOT_DB_USER": settings.copilot_db_user,
        "COPILOT_DB_PASSWORD": settings.copilot_db_password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing authentication database setting(s): {', '.join(missing)}"
        )

    return psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=settings.copilot_db_user,
        password=settings.copilot_db_password,
        sslmode=settings.copilot_db_sslmode,
    )


def create_schema(conn: Connection[Any]) -> None:
    """Apply the full auth schema idempotently on an open connection."""
    with conn.cursor() as cur:
        for statement in SCHEMA_DDL:
            cur.execute(statement)
    conn.commit()
    logger.info("Applied auth schema", extra={"statements": len(SCHEMA_DDL)})


def apply_schema(settings: Settings) -> None:
    """Connect using ``COPILOT_DB_*`` settings and apply the schema idempotently."""
    with connect(settings) as conn:
        create_schema(conn)
