"""PostgreSQL-backed user store for self-managed authentication.

Persists and reads ``users`` rows using the ``COPILOT_DB_*`` connection
settings (via :func:`rag_system.auth.schema.connect`). The store is the only
place that knows the row shape; callers receive :class:`UserRecord` instances.

A test double can be substituted by injecting a ``connection_factory`` — any
zero-argument callable returning a psycopg-style connection usable as a context
manager (commit + close on clean exit, rollback on exception).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from rag_system.auth import schema
from rag_system.auth.models import UserRecord
from rag_system.config import Settings
from rag_system.observability import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextlib import AbstractContextManager

logger = get_logger(__name__)

__all__ = ["EmailAlreadyExistsError", "PostgresUserStore"]

_COLUMNS = "id, email, password_hash, is_active, is_operator, created_at"

_INSERT_SQL = f"""
    INSERT INTO users (id, email, password_hash, is_active, is_operator, created_at)
    VALUES (%s, %s, %s, %s, %s, %s::timestamptz)
    RETURNING {_COLUMNS}
"""

_SELECT_BY_EMAIL_SQL = f"SELECT {_COLUMNS} FROM users WHERE lower(email) = lower(%s)"

_SELECT_BY_ID_SQL = f"SELECT {_COLUMNS} FROM users WHERE id = %s"

_UPDATE_PASSWORD_SQL = f"""
    UPDATE users SET password_hash = %s
    WHERE lower(email) = lower(%s)
    RETURNING {_COLUMNS}
"""

_SELECT_ANY_USER_SQL = "SELECT 1 FROM users LIMIT 1"

# Arbitrary constant key for the transaction-scoped advisory lock that
# serialises bootstrap-account creation (see create_bootstrap_user).
_BOOTSTRAP_LOCK_KEY = 0x1A9B_C0DE

_ADVISORY_XACT_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"

# Insert the account only while the table is still empty. Combined with the
# advisory lock above, this makes "create the first (bootstrap) account" atomic:
# a concurrent attempt blocks on the lock, then finds the row already present
# and inserts nothing (RETURNING yields zero rows).
_INSERT_BOOTSTRAP_SQL = f"""
    INSERT INTO users (id, email, password_hash, is_active, is_operator, created_at)
    SELECT %s, %s, %s, %s, %s, %s::timestamptz
    WHERE NOT EXISTS (SELECT 1 FROM users)
    RETURNING {_COLUMNS}
"""


class EmailAlreadyExistsError(Exception):
    """Raised when registering an email that already has an account."""


class PostgresUserStore:
    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[], "AbstractContextManager[Any]"] | None = None,
    ) -> None:
        self._settings = settings
        self._connection_factory = connection_factory or self._default_connection_factory

    def _default_connection_factory(self) -> "AbstractContextManager[Any]":
        return schema.connect(self._settings)

    def create_user(self, email: str, password_hash: str) -> UserRecord:
        """Insert a new active user, returning the stored record.

        Raises :class:`EmailAlreadyExistsError` when the (case-insensitive) email
        is already taken — detected either by a pre-check or by the unique-index
        violation, so concurrent registrations cannot create duplicates.
        """
        if self.get_by_email(email) is not None:
            raise EmailAlreadyExistsError(email)

        user_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        _INSERT_SQL,
                        (
                            user_id,
                            email,
                            password_hash,
                            True,
                            False,
                            created_at.isoformat(),
                        ),
                    )
                    row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - inspect for unique violation
            if _is_unique_violation(exc):
                raise EmailAlreadyExistsError(email) from exc
            raise
        logger.info("Registered user", extra={"user_id": user_id})
        return _row_to_user(row)

    def create_bootstrap_user(self, email: str, password_hash: str) -> UserRecord | None:
        """Atomically create the first account, or return ``None`` if one exists.

        Used when self-service registration is disabled: only the bootstrap
        account may be created. A transaction-scoped advisory lock serialises
        concurrent attempts, and the conditional insert (``WHERE NOT EXISTS``)
        guarantees a second racer inserts nothing and gets ``None`` — closing the
        check-then-act race in ``AuthService.register`` where two concurrent
        first-registrations could both create an account.
        """
        user_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_ADVISORY_XACT_LOCK_SQL, (_BOOTSTRAP_LOCK_KEY,))
                cur.execute(
                    _INSERT_BOOTSTRAP_SQL,
                    (
                        user_id,
                        email,
                        password_hash,
                        True,
                        False,
                        created_at.isoformat(),
                    ),
                )
                row = cur.fetchone()
        if row is None:
            return None
        logger.info("Registered bootstrap user", extra={"user_id": user_id})
        return _row_to_user(row)

    def get_by_email(self, email: str) -> UserRecord | None:
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_BY_EMAIL_SQL, (email,))
                row = cur.fetchone()
        return _row_to_user(row) if row else None

    def get_by_id(self, user_id: str) -> UserRecord | None:
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_BY_ID_SQL, (user_id,))
                row = cur.fetchone()
        return _row_to_user(row) if row else None

    def set_password(self, email: str, password_hash: str) -> UserRecord | None:
        """Set a user's password hash by (case-insensitive) email.

        Returns the updated record, or ``None`` when no account matches. Used by
        the admin password-reset tool so an operator locked out of their account
        can be recovered without a self-service email flow.
        """
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_UPDATE_PASSWORD_SQL, (password_hash, email))
                row = cur.fetchone()
        if row is None:
            return None
        record = _row_to_user(row)
        logger.info("Reset user password", extra={"user_id": record.id})
        return record

    def has_users(self) -> bool:
        """Return ``True`` when at least one account exists.

        Used to gate self-service registration: the first account may always be
        created (bootstrap), but once any user exists registration can be locked.
        """
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_ANY_USER_SQL)
                return cur.fetchone() is not None


def _row_to_user(row: Any) -> UserRecord:
    user_id, email, password_hash, is_active, is_operator, created_at = row
    return UserRecord(
        id=str(user_id),
        email=email,
        password_hash=password_hash,
        is_active=bool(is_active),
        is_operator=bool(is_operator),
        created_at=_coerce_utc(created_at),
    )


def _coerce_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_unique_violation(exc: Exception) -> bool:
    """Best-effort detection of a Postgres unique-constraint violation.

    Checks the psycopg ``UniqueViolation`` sqlstate (23505) without importing
    psycopg at module import time, then falls back to a message heuristic for
    injected test doubles.
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate == "23505":
        return True
    message = str(exc).lower()
    return "unique" in message and ("users_email" in message or "constraint" in message)
