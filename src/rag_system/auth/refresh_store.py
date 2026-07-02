"""PostgreSQL-backed refresh-token store (rotation + revocation).

Stores only the SHA-256 hash of each opaque refresh token. Supports the
rotation/reuse-detection flow in :class:`rag_system.auth.service.AuthService`:

* :meth:`create` — persist a freshly issued token hash with its expiry.
* :meth:`get_by_hash` — look a token up on refresh/logout.
* :meth:`revoke` — mark a single token revoked (rotation, logout).
* :meth:`revoke_all_for_user` — revoke every active token for a user
  (triggered on reuse detection, or for "log out everywhere").

Like :class:`rag_system.auth.store.PostgresUserStore`, a ``connection_factory``
can be injected for testing without a live database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from rag_system.auth import schema
from rag_system.config import Settings
from rag_system.observability import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextlib import AbstractContextManager

logger = get_logger(__name__)

__all__ = ["RefreshTokenRecord", "PostgresRefreshTokenStore"]


@dataclass(frozen=True)
class RefreshTokenRecord:
    id: str
    user_id: str
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.expires_at <= now


_COLUMNS = "id, user_id, token_hash, issued_at, expires_at, revoked_at"

_INSERT_SQL = f"""
    INSERT INTO refresh_tokens (id, user_id, token_hash, issued_at, expires_at, revoked_at)
    VALUES (%s, %s, %s, %s::timestamptz, %s::timestamptz, NULL)
    RETURNING {_COLUMNS}
"""

_SELECT_BY_HASH_SQL = f"SELECT {_COLUMNS} FROM refresh_tokens WHERE token_hash = %s"

_REVOKE_SQL = (
    "UPDATE refresh_tokens SET revoked_at = now() "
    "WHERE id = %s AND revoked_at IS NULL"
)

_REVOKE_ALL_SQL = (
    "UPDATE refresh_tokens SET revoked_at = now() "
    "WHERE user_id = %s AND revoked_at IS NULL"
)


class PostgresRefreshTokenStore:
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

    def create(
        self, user_id: str, token_hash: str, expires_at: datetime
    ) -> RefreshTokenRecord:
        token_id = str(uuid.uuid4())
        issued_at = datetime.now(timezone.utc)
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _INSERT_SQL,
                    (
                        token_id,
                        user_id,
                        token_hash,
                        issued_at.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                row = cur.fetchone()
        return _row_to_record(row)

    def get_by_hash(self, token_hash: str) -> RefreshTokenRecord | None:
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_BY_HASH_SQL, (token_hash,))
                row = cur.fetchone()
        return _row_to_record(row) if row else None

    def revoke(self, token_id: str) -> bool:
        """Revoke a single token, returning whether this call actually revoked it.

        The ``WHERE id = %s AND revoked_at IS NULL`` predicate makes this an
        atomic compare-and-set: under concurrent refreshes of the *same* token,
        exactly one ``UPDATE`` flips the row (``rowcount == 1``) and every other
        sees ``rowcount == 0``. Callers use this to ensure only the winner of the
        race issues a successor token pair.
        """
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_REVOKE_SQL, (token_id,))
                return cur.rowcount == 1

    def revoke_all_for_user(self, user_id: str) -> int:
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_REVOKE_ALL_SQL, (user_id,))
                count = cur.rowcount
        logger.info(
            "Revoked all refresh tokens for user",
            extra={"user_id": user_id, "revoked_count": count},
        )
        return count


def _row_to_record(row: Any) -> RefreshTokenRecord:
    token_id, user_id, token_hash, issued_at, expires_at, revoked_at = row
    return RefreshTokenRecord(
        id=str(token_id),
        user_id=str(user_id),
        token_hash=token_hash,
        issued_at=_coerce_utc(issued_at),
        expires_at=_coerce_utc(expires_at),
        revoked_at=_coerce_utc(revoked_at) if revoked_at is not None else None,
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
