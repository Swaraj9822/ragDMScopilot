"""Test support for the authentication subsystem (not collected by pytest).

The filename intentionally omits the ``test_`` prefix so pytest does not collect
it; auth tests import it by module name, matching the repo convention for shared
test fakes (see ``observability_tracing_store_double.py`` and its use in the
tracing property tests).

Provides:

* :func:`make_settings` — a fully-populated :class:`~rag_system.config.Settings`
  with a known JWT secret, built without depending on the real ``.env``.
* :class:`InMemoryUserStore` / :class:`InMemoryRefreshStore` — drop-in doubles
  implementing exactly the methods :class:`~rag_system.auth.service.AuthService`
  calls, so the service's rotation / reuse-detection logic can be exercised
  without PostgreSQL.
* :class:`FakeAuthDB` — a psycopg-shaped connection double that dispatches on
  the store SQL, so the *real* ``PostgresUserStore`` / ``PostgresRefreshTokenStore``
  code (row coercion, unique-violation handling) is exercised end to end.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from rag_system.auth.models import UserRecord
from rag_system.auth.refresh_store import RefreshTokenRecord
from rag_system.auth.store import EmailAlreadyExistsError
from rag_system.config import Settings

__all__ = [
    "make_settings",
    "InMemoryUserStore",
    "InMemoryRefreshStore",
    "FakeUniqueViolation",
    "FakeAuthDB",
]

# Required Settings fields have no defaults, so supply harmless placeholders.
_REQUIRED = {
    "RAG_S3_BUCKET": "test-bucket",
    "RAG_INGESTION_QUEUE_URL": "https://sqs.test.invalid/queue",
    "LLAMA_CLOUD_API_KEY": "llx-test",
    "PINECONE_API_KEY": "pc-test",
    "PINECONE_INDEX_NAME": "test-index",
}

_AUTH_DEFAULTS = {
    "RAG_JWT_SECRET_KEY": "unit-test-secret-key-not-for-production-use",
    "RAG_JWT_ISSUER": "production-rag-tests",
    "RAG_AUTH_ENABLED": True,
    "RAG_AUTH_RATE_LIMIT_PER_MINUTE": 0,  # disable throttling in most tests
    # The refresh cookie must round-trip over the TestClient's plain-http
    # transport, so disable Secure and use SameSite=Lax (httpx only replays
    # Secure cookies over https).
    "RAG_AUTH_COOKIE_SECURE": False,
    "RAG_AUTH_COOKIE_SAMESITE": "lax",
    # Force the DB connection settings unset so tests never inherit real
    # COPILOT_DB_* values from a local .env (which would let schema.connect()
    # reach a live database). Tests that need storage use injected doubles.
    "COPILOT_DB_HOST": None,
    "COPILOT_DB_NAME": None,
    "COPILOT_DB_USER": None,
    "COPILOT_DB_PASSWORD": None,
}


def make_settings(**overrides) -> Settings:
    """Build a Settings instance with test-safe values plus any *overrides*.

    Overrides use the field *alias* names (e.g. ``RAG_AUTH_ENABLED``) so they
    line up with the environment variable names used throughout the app.
    """
    values = {**_REQUIRED, **_AUTH_DEFAULTS, **overrides}
    return Settings(**values)  # type: ignore[arg-type]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryUserStore:
    """In-memory stand-in matching the ``PostgresUserStore`` interface."""

    def __init__(self) -> None:
        self._by_id: dict[str, UserRecord] = {}
        self._by_email: dict[str, UserRecord] = {}

    # -- production interface ------------------------------------------------

    def has_users(self) -> bool:
        return bool(self._by_id)

    def get_by_email(self, email: str) -> UserRecord | None:
        return self._by_email.get(email.strip().lower())

    def get_by_id(self, user_id: str) -> UserRecord | None:
        return self._by_id.get(user_id)

    def create_user(self, email: str, password_hash: str) -> UserRecord:
        key = email.strip().lower()
        if key in self._by_email:
            raise EmailAlreadyExistsError(email)
        record = UserRecord(
            id=str(uuid.uuid4()),
            email=email,
            password_hash=password_hash,
            is_active=True,
            created_at=_now(),
        )
        self._by_id[record.id] = record
        self._by_email[key] = record
        return record

    def create_bootstrap_user(self, email: str, password_hash: str) -> UserRecord | None:
        # Models the atomic "insert only if the table is empty" guard: once any
        # user exists, the bootstrap insert affects no rows and returns None.
        if self._by_id:
            return None
        return self.create_user(email, password_hash)

    # -- test helpers --------------------------------------------------------

    def add(self, record: UserRecord) -> UserRecord:
        self._by_id[record.id] = record
        self._by_email[record.email.strip().lower()] = record
        return record

    def deactivate(self, user_id: str) -> None:
        record = self._by_id[user_id]
        updated = dataclasses.replace(record, is_active=False)
        self._by_id[user_id] = updated
        self._by_email[updated.email.strip().lower()] = updated


class InMemoryRefreshStore:
    """In-memory stand-in matching the ``PostgresRefreshTokenStore`` interface.

    Revoked tokens are kept (not deleted) so the service's reuse-detection path
    can still look them up by hash and see ``is_revoked``.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, RefreshTokenRecord] = {}

    def create(
        self, user_id: str, token_hash: str, expires_at: datetime
    ) -> RefreshTokenRecord:
        record = RefreshTokenRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            token_hash=token_hash,
            issued_at=_now(),
            expires_at=expires_at,
            revoked_at=None,
        )
        self._by_id[record.id] = record
        return record

    def get_by_hash(self, token_hash: str) -> RefreshTokenRecord | None:
        for record in self._by_id.values():
            if record.token_hash == token_hash:
                return record
        return None

    def revoke(self, token_id: str) -> bool:
        record = self._by_id.get(token_id)
        if record is not None and record.revoked_at is None:
            self._by_id[token_id] = dataclasses.replace(record, revoked_at=_now())
            return True
        return False

    def revoke_all_for_user(self, user_id: str) -> int:
        count = 0
        for token_id, record in list(self._by_id.items()):
            if record.user_id == user_id and record.revoked_at is None:
                self._by_id[token_id] = dataclasses.replace(record, revoked_at=_now())
                count += 1
        return count

    # -- test helpers --------------------------------------------------------

    def active_count(self, user_id: str) -> int:
        return sum(
            1
            for r in self._by_id.values()
            if r.user_id == user_id and r.revoked_at is None
        )


# ---------------------------------------------------------------------------
# psycopg-shaped connection double for exercising the real store code
# ---------------------------------------------------------------------------


class FakeUniqueViolation(Exception):
    """Mimics psycopg's UniqueViolation (sqlstate 23505) for the store tests."""

    sqlstate = "23505"


class _FakeCursor:
    def __init__(self, db: "FakeAuthDB") -> None:
        self._db = db
        self._result = None
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        s = " ".join(sql.split())
        if "pg_advisory_xact_lock" in s:
            # Serialisation primitive; the single-threaded double is already
            # serial, so the lock is a no-op here.
            self._result = (1,)
        elif "INSERT INTO users" in s and "WHERE NOT EXISTS" in s:
            self._insert_bootstrap_user(params)
        elif "INSERT INTO users" in s:
            self._insert_user(params)
        elif "FROM users WHERE lower(email)" in s:
            self._result = self._db.find_user_by_email(params[0])
        elif "FROM users WHERE id" in s:
            self._result = self._db.find_user_by_id(params[0])
        elif "SELECT 1 FROM users" in s:
            self._result = (1,) if self._db.users else None
        elif "INSERT INTO refresh_tokens" in s:
            self._insert_refresh(params)
        elif "FROM refresh_tokens WHERE token_hash" in s:
            self._result = self._db.find_refresh_by_hash(params[0])
        elif "UPDATE refresh_tokens SET revoked_at" in s and "WHERE id" in s:
            self.rowcount = self._db.revoke_refresh(params[0])
        elif "UPDATE refresh_tokens SET revoked_at" in s and "WHERE user_id" in s:
            self.rowcount = self._db.revoke_all_refresh(params[0])
        else:  # pragma: no cover - unexpected SQL is a test bug
            raise AssertionError(f"unhandled SQL in FakeAuthDB: {s!r}")

    def fetchone(self):
        return self._result

    def _insert_user(self, params: tuple) -> None:
        if self._db.force_unique_violation:
            raise FakeUniqueViolation("duplicate key value violates users_email_lower_key")
        user_id, email, password_hash, is_active, created_at = params
        row = (user_id, email, password_hash, is_active, created_at)
        self._db.users[user_id] = row
        self._result = row

    def _insert_bootstrap_user(self, params: tuple) -> None:
        # Conditional insert: only succeeds while the users table is empty
        # (WHERE NOT EXISTS). Otherwise RETURNING yields no row.
        if self._db.users:
            self._result = None
            return
        user_id, email, password_hash, is_active, created_at = params
        row = (user_id, email, password_hash, is_active, created_at)
        self._db.users[user_id] = row
        self._result = row

    def _insert_refresh(self, params: tuple) -> None:
        token_id, user_id, token_hash, issued_at, expires_at = params
        row = [token_id, user_id, token_hash, issued_at, expires_at, None]
        self._db.refresh[token_id] = row
        self._result = tuple(row)


class _FakeConnection:
    def __init__(self, db: "FakeAuthDB") -> None:
        self._db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeAuthDB:
    """A tiny in-memory database with a psycopg-shaped ``connection_factory``."""

    def __init__(self) -> None:
        self.users: dict[str, tuple] = {}
        self.refresh: dict[str, list] = {}
        self.force_unique_violation = False

    def connection_factory(self) -> _FakeConnection:
        return _FakeConnection(self)

    # -- query helpers used by the fake cursor ------------------------------

    def find_user_by_email(self, email: str):
        for row in self.users.values():
            if row[1].strip().lower() == email.strip().lower():
                return row
        return None

    def find_user_by_id(self, user_id: str):
        return self.users.get(user_id)

    def find_refresh_by_hash(self, token_hash: str):
        for row in self.refresh.values():
            if row[2] == token_hash:
                return tuple(row)
        return None

    def revoke_refresh(self, token_id: str) -> int:
        row = self.refresh.get(token_id)
        if row is not None and row[5] is None:
            row[5] = _now()
            return 1
        return 0

    def revoke_all_refresh(self, user_id: str) -> int:
        count = 0
        for row in self.refresh.values():
            if row[1] == user_id and row[5] is None:
                row[5] = _now()
                count += 1
        return count
