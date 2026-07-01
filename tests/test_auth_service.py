"""Tests for AuthService: registration policy, authentication, and the
refresh-token rotation / reuse-detection flow.

Uses in-memory store doubles so the service logic is exercised without a live
PostgreSQL database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rag_system.auth.service import (
    AuthService,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RegistrationClosedError,
)
from rag_system.auth.store import EmailAlreadyExistsError
from rag_system.auth.tokens import decode_token, hash_refresh_token

from auth_doubles import InMemoryRefreshStore, InMemoryUserStore, make_settings


def _service(**setting_overrides):
    settings = make_settings(**setting_overrides)
    users = InMemoryUserStore()
    refresh = InMemoryRefreshStore()
    service = AuthService(settings, store=users, refresh_store=refresh)
    return service, users, refresh, settings


# --- registration policy ---------------------------------------------------


def test_register_creates_user_and_hashes_password():
    service, users, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    record = service.register("new@example.com", "password123")
    assert record.email == "new@example.com"
    assert record.password_hash != "password123"
    assert users.get_by_email("new@example.com") is not None


def test_first_registration_allowed_even_when_registration_closed():
    """Bootstrap: the very first account is permitted so an operator can set up."""
    service, users, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=False)
    record = service.register("first@example.com", "password123")
    assert users.get_by_id(record.id) is not None


def test_registration_closes_after_first_user_when_disabled():
    service, _, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=False)
    service.register("first@example.com", "password123")
    with pytest.raises(RegistrationClosedError):
        service.register("second@example.com", "password123")


def test_registration_stays_open_when_enabled():
    service, _, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    service.register("first@example.com", "password123")
    second = service.register("second@example.com", "password123")
    assert second.email == "second@example.com"


def test_duplicate_email_raises():
    service, _, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    service.register("dup@example.com", "password123")
    with pytest.raises(EmailAlreadyExistsError):
        service.register("dup@example.com", "password123")


# --- authentication --------------------------------------------------------


def test_authenticate_success():
    service, _, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    service.register("user@example.com", "password123")
    user = service.authenticate("user@example.com", "password123")
    assert user.email == "user@example.com"


def test_authenticate_wrong_password_raises_invalid_credentials():
    service, _, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    service.register("user@example.com", "password123")
    with pytest.raises(InvalidCredentialsError):
        service.authenticate("user@example.com", "wrong-password")


def test_authenticate_unknown_email_raises_invalid_credentials():
    """Unknown email must fail the same way as a wrong password (no enumeration)."""
    service, _, _, _ = _service()
    with pytest.raises(InvalidCredentialsError):
        service.authenticate("ghost@example.com", "password123")


def test_authenticate_inactive_user_raises_inactive_error():
    service, users, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    record = service.register("user@example.com", "password123")
    users.deactivate(record.id)
    with pytest.raises(InactiveUserError):
        service.authenticate("user@example.com", "password123")


# --- login / token issuance ------------------------------------------------


def test_login_issues_decodable_access_token_and_persists_refresh():
    service, _, refresh, settings = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    user = service.register("user@example.com", "password123")
    pair = service.login("user@example.com", "password123")

    claims = decode_token(settings, pair.access_token)
    assert claims["sub"] == user.id
    assert claims["email"] == "user@example.com"
    assert pair.token_type == "bearer"
    # The refresh token is persisted only as a hash, never in plaintext.
    stored = refresh.get_by_hash(hash_refresh_token(pair.refresh_token))
    assert stored is not None and stored.revoked_at is None


def test_login_wrong_password_raises():
    service, _, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    service.register("user@example.com", "password123")
    with pytest.raises(InvalidCredentialsError):
        service.login("user@example.com", "nope")


# --- refresh rotation + reuse detection ------------------------------------


def test_refresh_rotates_and_revokes_old_token():
    service, _, refresh, settings = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    user = service.register("user@example.com", "password123")
    first = service.login("user@example.com", "password123")

    rotated = service.refresh(first.refresh_token)
    assert rotated.refresh_token != first.refresh_token

    # Old token is now revoked; new token is active.
    old = refresh.get_by_hash(hash_refresh_token(first.refresh_token))
    new = refresh.get_by_hash(hash_refresh_token(rotated.refresh_token))
    assert old is not None and old.is_revoked
    assert new is not None and not new.is_revoked
    # New access token is valid and identifies the same user.
    assert decode_token(settings, rotated.access_token)["sub"] == user.id


def test_refresh_unknown_token_raises():
    service, _, _, _ = _service()
    with pytest.raises(InvalidRefreshTokenError):
        service.refresh("totally-unknown-token")


def test_refresh_reuse_of_revoked_token_revokes_entire_family():
    service, _, refresh, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    user = service.register("user@example.com", "password123")
    first = service.login("user@example.com", "password123")
    service.refresh(first.refresh_token)  # rotates; `first` is now revoked

    # Replaying the already-rotated token is treated as theft.
    with pytest.raises(InvalidRefreshTokenError):
        service.refresh(first.refresh_token)

    # Both the attacker's and the victim's tokens are now dead.
    assert refresh.active_count(user.id) == 0


def test_refresh_expired_token_raises():
    service, users, refresh, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    user = service.register("user@example.com", "password123")
    plain = "expired-refresh-token-value"
    refresh.create(
        user.id,
        hash_refresh_token(plain),
        datetime.now(timezone.utc) - timedelta(days=1),
    )
    with pytest.raises(InvalidRefreshTokenError):
        service.refresh(plain)


def test_refresh_for_deactivated_user_raises():
    service, users, _, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    user = service.register("user@example.com", "password123")
    first = service.login("user@example.com", "password123")
    users.deactivate(user.id)
    with pytest.raises(InvalidRefreshTokenError):
        service.refresh(first.refresh_token)


# --- logout ----------------------------------------------------------------


def test_logout_revokes_refresh_token():
    service, _, refresh, _ = _service(RAG_AUTH_ALLOW_REGISTRATION=True)
    service.register("user@example.com", "password123")
    pair = service.login("user@example.com", "password123")

    service.logout(pair.refresh_token)
    stored = refresh.get_by_hash(hash_refresh_token(pair.refresh_token))
    assert stored is not None and stored.is_revoked
    # After logout the token can no longer be refreshed.
    with pytest.raises(InvalidRefreshTokenError):
        service.refresh(pair.refresh_token)


def test_logout_unknown_token_is_a_noop():
    service, _, _, _ = _service()
    service.logout("never-issued")  # must not raise
