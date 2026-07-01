"""Tests for the get_current_user FastAPI dependency."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from rag_system.auth.dependencies import get_current_user
from rag_system.auth.models import UserRecord
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
