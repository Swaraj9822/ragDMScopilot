"""Tests for the PostgreSQL-backed user/refresh stores.

The real store classes run against a psycopg-shaped connection double
(``FakeAuthDB``) so row coercion, duplicate handling, and revocation SQL
dispatch are all exercised without a live database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rag_system.auth.refresh_store import PostgresRefreshTokenStore, RefreshTokenRecord
from rag_system.auth.store import EmailAlreadyExistsError, PostgresUserStore

from auth_doubles import FakeAuthDB, make_settings


# --- RefreshTokenRecord value semantics ------------------------------------


def test_record_is_revoked_reflects_revoked_at():
    base = dict(
        id="t1",
        user_id="u1",
        token_hash="h",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert RefreshTokenRecord(revoked_at=None, **base).is_revoked is False
    assert RefreshTokenRecord(revoked_at=datetime.now(timezone.utc), **base).is_revoked is True


def test_record_is_expired_boundary():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rec = RefreshTokenRecord(
        id="t",
        user_id="u",
        token_hash="h",
        issued_at=now - timedelta(days=1),
        expires_at=now,
        revoked_at=None,
    )
    assert rec.is_expired(now=now) is True  # expiry is inclusive (<=)
    assert rec.is_expired(now=now - timedelta(seconds=1)) is False
    assert rec.is_expired(now=now + timedelta(seconds=1)) is True


# --- PostgresUserStore -----------------------------------------------------


def _user_store():
    db = FakeAuthDB()
    store = PostgresUserStore(make_settings(), connection_factory=db.connection_factory)
    return store, db


def test_create_user_persists_and_reads_back():
    store, _ = _user_store()
    created = store.create_user("User@Example.com", "hashed-pw")
    assert created.email == "User@Example.com"
    assert created.is_active is True
    assert created.created_at.tzinfo is not None  # coerced to tz-aware UTC

    fetched = store.get_by_id(created.id)
    assert fetched is not None and fetched.id == created.id


def test_new_users_are_non_operators_by_default():
    store, _ = _user_store()
    created = store.create_user("plain@example.com", "h")
    assert created.is_operator is False
    fetched = store.get_by_id(created.id)
    assert fetched is not None and fetched.is_operator is False


def test_stored_operator_flag_is_read_back():
    """The store selects and coerces the is_operator column, so a stored
    operator round-trips (regression: the column was previously not selected)."""
    store, db = _user_store()
    created = store.create_user("op@example.com", "h")
    # Simulate an elevated stored row (id, email, hash, is_active, is_operator, created_at).
    row = db.users[created.id]
    db.users[created.id] = (row[0], row[1], row[2], row[3], True, row[5])

    fetched = store.get_by_id(created.id)
    assert fetched is not None and fetched.is_operator is True
    by_email = store.get_by_email("op@example.com")
    assert by_email is not None and by_email.is_operator is True


def test_get_by_email_is_case_insensitive():
    store, _ = _user_store()
    store.create_user("Mixed@Case.com", "h")
    assert store.get_by_email("mixed@case.com") is not None
    assert store.get_by_email("MIXED@CASE.COM") is not None


def test_get_by_email_and_id_return_none_when_absent():
    store, _ = _user_store()
    assert store.get_by_email("nobody@example.com") is None
    assert store.get_by_id("no-such-id") is None


def test_create_duplicate_email_raises_via_precheck():
    store, _ = _user_store()
    store.create_user("dup@example.com", "h")
    with pytest.raises(EmailAlreadyExistsError):
        store.create_user("dup@example.com", "h")


def test_create_maps_unique_violation_to_email_exists():
    """Even if the pre-check is bypassed (race), a 23505 maps to the domain error."""
    store, db = _user_store()
    db.force_unique_violation = True
    with pytest.raises(EmailAlreadyExistsError):
        store.create_user("racy@example.com", "h")


def test_has_users_reflects_population():
    store, _ = _user_store()
    assert store.has_users() is False
    store.create_user("someone@example.com", "h")
    assert store.has_users() is True


def test_create_bootstrap_user_creates_when_table_empty():
    store, _ = _user_store()
    record = store.create_bootstrap_user("first@example.com", "h")
    assert record is not None and record.email == "first@example.com"
    assert store.get_by_id(record.id) is not None


def test_create_bootstrap_user_returns_none_when_table_not_empty():
    store, _ = _user_store()
    store.create_user("existing@example.com", "h")
    # The conditional insert affects no rows, so no second account is created.
    assert store.create_bootstrap_user("second@example.com", "h") is None
    assert store.get_by_email("second@example.com") is None


# --- PostgresRefreshTokenStore ---------------------------------------------


def _refresh_store():
    db = FakeAuthDB()
    store = PostgresRefreshTokenStore(make_settings(), connection_factory=db.connection_factory)
    return store, db


def test_refresh_create_and_lookup_by_hash():
    store, _ = _refresh_store()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    created = store.create("user-1", "hash-abc", expires)
    assert created.user_id == "user-1"
    assert created.revoked_at is None

    found = store.get_by_hash("hash-abc")
    assert found is not None and found.id == created.id
    assert store.get_by_hash("missing") is None


def test_refresh_revoke_marks_single_token():
    store, _ = _refresh_store()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    created = store.create("user-1", "hash-abc", expires)
    store.revoke(created.id)
    assert store.get_by_hash("hash-abc").is_revoked is True


def test_refresh_revoke_is_atomic_cas_returning_true_then_false():
    # The conditional UPDATE reports it revoked exactly once; a repeat (e.g. a
    # concurrent refresh of the same token) reports False and issues nothing.
    store, _ = _refresh_store()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    created = store.create("user-1", "hash-abc", expires)
    assert store.revoke(created.id) is True
    assert store.revoke(created.id) is False


def test_refresh_revoke_all_for_user_counts_only_active():
    store, _ = _refresh_store()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    store.create("user-1", "h1", expires)
    store.create("user-1", "h2", expires)
    store.create("user-2", "h3", expires)
    revoked = store.revoke_all_for_user("user-1")
    assert revoked == 2
    assert store.get_by_hash("h3").is_revoked is False  # other user untouched
    # Revoking again is a no-op (nothing active left).
    assert store.revoke_all_for_user("user-1") == 0


def test_refresh_delete_expired_removes_only_past_expiry():
    store, _ = _refresh_store()
    now = datetime.now(timezone.utc)
    store.create("user-1", "expired-1", now - timedelta(seconds=1))
    store.create("user-1", "expired-2", now - timedelta(days=5))
    store.create("user-2", "live", now + timedelta(days=30))

    deleted = store.delete_expired()

    assert deleted == 2
    # Expired rows are gone; the still-valid token is untouched.
    assert store.get_by_hash("expired-1") is None
    assert store.get_by_hash("expired-2") is None
    assert store.get_by_hash("live") is not None
    # Running again with nothing expired removes nothing.
    assert store.delete_expired() == 0

