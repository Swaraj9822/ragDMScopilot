"""FastAPI dependencies for authentication.

Provides the shared :class:`AuthService` singleton, a bearer-token security
scheme, and ``get_current_user`` — the dependency every protected endpoint uses
to require a valid token and resolve the calling user.

When ``settings.auth_enabled`` is ``False`` the dependency short-circuits and
returns an anonymous user, so trusted single-user deployments and tests can run
without tokens while the same code paths stay in place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from rag_system.auth.models import UserPublic
from rag_system.auth.service import AuthService
from rag_system.auth.tokens import TokenError, decode_token
from rag_system.config import Settings, get_settings

__all__ = ["get_auth_service", "get_current_user", "bearer_scheme"]

# auto_error=False so a missing header yields our own 401 (with the
# WWW-Authenticate hint) rather than FastAPI's default 403.
bearer_scheme = HTTPBearer(auto_error=False)

_ANONYMOUS = UserPublic(
    id="anonymous",
    email="anonymous@localhost",
    is_active=True,
    created_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
)


@lru_cache
def get_auth_service() -> AuthService:
    return AuthService(get_settings())


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
) -> UserPublic:
    """Resolve the authenticated user from the bearer token, or raise 401.

    Steps: require a bearer credential, verify/decode the token, then confirm
    the subject still maps to an active user. A deactivated or deleted user is
    rejected even with an otherwise-valid token.
    """
    if not settings.auth_enabled:
        return _ANONYMOUS

    if credentials is None or not credentials.credentials:
        raise _unauthorized("Not authenticated.")

    try:
        claims = decode_token(settings, credentials.credentials)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    user_id = claims.get("sub")
    if not user_id:
        raise _unauthorized("Invalid authentication token.")

    user = auth_service.get_user(user_id)
    if user is None:
        raise _unauthorized("User no longer exists.")
    if not user.is_active:
        raise _unauthorized("This account is disabled.")

    return user.to_public()
