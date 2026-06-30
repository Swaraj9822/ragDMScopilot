"""Authentication HTTP endpoints: ``/auth/register``, ``/auth/login``, ``/auth/me``.

These routes are public (registration and login must work before a token
exists); ``/auth/me`` is protected and echoes the authenticated user. Mount with
``app.include_router(auth_router)`` from the main application.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from rag_system.auth.dependencies import get_auth_service, get_current_user
from rag_system.auth.models import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserPublic,
)
from rag_system.auth.service import (
    AuthService,
    EmailAlreadyExistsError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RegistrationClosedError,
)
from rag_system.auth.tokens import TokenError
from rag_system.config import get_settings
from rag_system.observability import get_logger
from rag_system.rate_limit import SlidingWindowRateLimiter, rate_limit

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Shared per-process limiter for credential-bearing endpoints. A per-minute
# allowance of 0 disables throttling. Built once at import from settings; the
# dependency it backs reads the client identity per request.
_auth_rpm = get_settings().auth_rate_limit_per_minute
_auth_limiter: SlidingWindowRateLimiter | None = (
    SlidingWindowRateLimiter(limit=_auth_rpm, window_seconds=60.0)
    if _auth_rpm > 0
    else None
)


def _auth_rate_limit(scope: str):
    """Dependency list throttling *scope*, or empty when limiting is disabled."""
    if _auth_limiter is None:
        return []
    return [Depends(rate_limit(_auth_limiter, scope=scope))]


@router.post(
    "/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    dependencies=_auth_rate_limit("register"),
)
def register(
    request: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> UserPublic:
    """Create a new account and return its public profile."""
    try:
        user = auth_service.register(request.email, request.password)
    except RegistrationClosedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-service registration is disabled.",
        ) from exc
    except EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc
    return user.to_public()


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=_auth_rate_limit("login"),
)
def login(
    request: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Exchange email + password for a signed access token."""
    try:
        return auth_service.login(request.email, request.password)
    except (InvalidCredentialsError, InactiveUserError) as exc:
        # Use 401 for both so a disabled account is not distinguishable from a
        # wrong password to an unauthenticated caller.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except TokenError as exc:
        # Misconfiguration (e.g. missing secret) — not the client's fault.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=_auth_rate_limit("refresh"),
)
def refresh(
    request: RefreshRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Exchange a valid refresh token for a new access + refresh token pair.

    The presented refresh token is rotated (revoked) and replaced. A revoked or
    reused token is rejected with 401; reuse additionally revokes the user's
    whole token family server-side.
    """
    try:
        return auth_service.refresh(request.refresh_token)
    except InvalidRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def logout(
    request: LogoutRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> Response:
    """Revoke a refresh token so it can no longer be rotated.

    Idempotent — revoking an unknown or already-revoked token still returns 204.
    Access tokens already issued remain valid until they expire (they are
    stateless); keep their TTL short.
    """
    auth_service.logout(request.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserPublic)
def me(current_user: UserPublic = Depends(get_current_user)) -> UserPublic:
    """Return the currently authenticated user."""
    return current_user
