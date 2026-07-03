"""Tests for the get_current_user FastAPI dependency."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from rag_system.auth.dependencies import (
    get_current_user,
    require_operator,
    resolve_is_operator,
)
from rag_system.auth.models import UserPublic, UserRecord
from rag_system.auth.tokens import create_access_token

from auth_doubles import make_settings


class _StubAuthService:
    def __init__(self, user: UserRecord | None):
        self._user = user

    def get_user(self, user_id: str) -> UserRecord | None:
        if self._user is not None and self._user.id == user_id:
            return self._user
        return None


def _active_user(user_id: str = "user-1") -> UserRecord:
    return UserRecord(
        id=user_id,
        email="user@example.com",
        password_hash="x",
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_auth_disabled_returns_anonymous_without_token():
    settings = make_settings(RAG_AUTH_ENABLED=False)
    user = get_current_user(credentials=None, settings=settings, auth_service=_StubAuthService(None))
    assert user.id == "anonymous"


def test_valid_token_resolves_active_user():
    settings = make_settings()
    user = _active_user()
    token = create_access_token(settings, subject=user.id, email=user.email).token
    resolved = get_current_user(
        credentials=_bearer(token), settings=settings, auth_service=_StubAuthService(user)
    )
    assert resolved.id == user.id
    assert not hasattr(resolved, "password_hash")


def test_missing_credentials_raises_401():
    settings = make_settings()
    with pytest.raises(HTTPException) as exc:
        get_current_user(credentials=None, settings=settings, auth_service=_StubAuthService(None))
    assert exc.value.status_code == 401
    assert exc.value.headers.get("WWW-Authenticate") == "Bearer"


def test_invalid_token_raises_401():
    settings = make_settings()
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=_bearer("garbage.token"),
            settings=settings,
            auth_service=_StubAuthService(None),
        )
    assert exc.value.status_code == 401


def test_valid_token_but_user_deleted_raises_401():
    settings = make_settings()
    token = create_access_token(settings, subject="user-1", email="user@example.com").token
    # Service returns None -> user no longer exists.
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=_bearer(token), settings=settings, auth_service=_StubAuthService(None)
        )
    assert exc.value.status_code == 401


def test_valid_token_but_user_inactive_raises_401():
    settings = make_settings()
    inactive = UserRecord(
        id="user-1",
        email="user@example.com",
        password_hash="x",
        is_active=False,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    token = create_access_token(settings, subject=inactive.id, email=inactive.email).token
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=_bearer(token),
            settings=settings,
            auth_service=_StubAuthService(inactive),
        )
    assert exc.value.status_code == 401


# --- Operator authorization (require_operator / resolve_is_operator) -------


def _public(
    *, email: str = "user@example.com", is_operator: bool = False
) -> UserPublic:
    return UserPublic(
        id="user-1",
        email=email,
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        is_operator=is_operator,
    )


def test_resolve_is_operator_true_for_stored_flag():
    settings = make_settings()  # empty allow-list
    assert resolve_is_operator(_public(is_operator=True), settings) is True


def test_resolve_is_operator_true_for_allow_list_membership():
    # Allow-list normalizes case/whitespace; a member with a False stored flag
    # still resolves to operator.
    settings = make_settings(RAG_OPERATOR_EMAILS=" Ops@Example.com , other@x.com ")
    user = _public(email="ops@example.com", is_operator=False)
    assert resolve_is_operator(user, settings) is True


def test_resolve_is_operator_false_when_neither():
    settings = make_settings(RAG_OPERATOR_EMAILS="ops@example.com")
    user = _public(email="someone-else@example.com", is_operator=False)
    assert resolve_is_operator(user, settings) is False


def test_require_operator_rejects_non_operator_with_403():
    settings = make_settings(RAG_OPERATOR_EMAILS="ops@example.com")
    non_operator = _public(email="user@example.com", is_operator=False)
    with pytest.raises(HTTPException) as exc:
        require_operator(user=non_operator, settings=settings)
    assert exc.value.status_code == 403
    assert exc.value.detail == "operator_required"


def test_require_operator_allows_stored_operator():
    settings = make_settings()  # empty allow-list
    operator = _public(is_operator=True)
    resolved = require_operator(user=operator, settings=settings)
    assert resolved.is_operator is True


def test_require_operator_allows_allow_listed_operator_and_reflects_flag():
    settings = make_settings(RAG_OPERATOR_EMAILS="ops@example.com")
    # Stored flag is False, but the allow-list grants operator status; the
    # returned user reflects the resolved flag.
    user = _public(email="ops@example.com", is_operator=False)
    resolved = require_operator(user=user, settings=settings)
    assert resolved.is_operator is True


def test_require_operator_allows_anonymous_when_auth_disabled():
    settings = make_settings(RAG_AUTH_ENABLED=False)
    anonymous = _public(email="anonymous@localhost", is_operator=False)
    resolved = require_operator(user=anonymous, settings=settings)
    assert resolved is anonymous
