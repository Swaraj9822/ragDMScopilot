"""Authentication HTTP endpoints: ``/auth/register``, ``/auth/login``, ``/auth/me``.

These routes are public (registration and login must work before a token
exists); ``/auth/me`` is protected and echoes the authenticated user. Mount with
``app.include_router(auth_router)`` from the main application.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status

from rag_system.auth.dependencies import (
    get_auth_service,
    get_current_user,
    resolve_is_operator,
)
from rag_system.auth.models import (
    AccessTokenResponse,
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
from rag_system.config import Settings, get_settings
from rag_system.observability import get_logger
from rag_system.rate_limit import SlidingWindowRateLimiter, rate_limit

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Name of the httpOnly cookie carrying the refresh token. Scoped to ``/auth``
#: so the browser only sends it to the refresh/logout endpoints, not on every
#: API call.
REFRESH_COOKIE_NAME = "refresh_token"
_REFRESH_COOKIE_PATH = "/auth"


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach the rotated refresh token as an httpOnly cookie.

    ``httponly`` keeps it out of ``document.cookie`` (and thus out of reach of an
    XSS payload); ``secure``/``samesite`` are configured for the deployment's
    origin topology (see ``Settings.auth_cookie_*``).
    """
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        domain=settings.auth_cookie_domain,
        path=_REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=_REFRESH_COOKIE_PATH,
        domain=settings.auth_cookie_domain,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )


def _access_body(pair: TokenResponse) -> AccessTokenResponse:
    return AccessTokenResponse(
        access_token=pair.access_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


def _origin_from_referer(referer: str | None) -> str | None:
    """Derive a ``scheme://host[:port]`` origin from a Referer header, or None."""
    if not referer:
        return None
    parts = urlsplit(referer)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def verify_trusted_origin(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject cross-site (CSRF) requests to cookie-authenticated auth endpoints.

    The refresh token rides in an httpOnly cookie the browser attaches
    automatically, so ``/auth/refresh`` and ``/auth/logout`` are reachable by a
    cross-site forgery. This guard enforces the standard Origin/Referer check:

    * When the browser supplies an ``Origin`` header (always present on a
      cross-site ``POST``) — or, failing that, a ``Referer`` — it must match a
      trusted origin: one of the configured CORS origins, or the request's own
      host (same-origin). A mismatch is refused with ``403``.
    * A request with neither header (e.g. a non-browser client that sends the
      refresh token in the JSON body rather than the cookie) is allowed: the
      cookie-CSRF vector requires a browser, and browsers always send ``Origin``
      on a cross-site state-changing request.

    Host comparison for the same-origin case uses ``netloc`` (host:port) only, so
    it still holds behind a TLS-terminating proxy that presents a different
    scheme to the app.
    """
    origin = request.headers.get("origin") or _origin_from_referer(
        request.headers.get("referer")
    )
    if origin is None:
        return

    normalized = origin.rstrip("/").lower()
    allowed = {o.rstrip("/").lower() for o in settings.cors_allow_origins_list}
    if normalized in allowed:
        return

    origin_netloc = urlsplit(origin).netloc.lower()
    if origin_netloc and origin_netloc == request.url.netloc.lower():
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="cross-origin request rejected",
    )

# Shared per-process limiters for credential-bearing endpoints, keyed by the
# configured per-minute allowance. A per-minute allowance of 0 disables
# throttling (cached as None). Keying by the rate means a settings reload that
# changes the allowance rebuilds the limiter instead of serving a stale one; the
# limiter is still resolved lazily on first request (not at import) so importing
# this module never reads settings.
_auth_limiters: dict[int, SlidingWindowRateLimiter | None] = {}


def _get_auth_limiter() -> SlidingWindowRateLimiter | None:
    """Build (once per distinct rate) and return the shared auth limiter.

    Returns ``None`` when throttling is disabled (per-minute allowance 0).
    Rebuilds automatically if the configured allowance changes.
    """
    rpm = get_settings().auth_rate_limit_per_minute
    if rpm not in _auth_limiters:
        _auth_limiters[rpm] = (
            SlidingWindowRateLimiter(limit=rpm, window_seconds=60.0) if rpm > 0 else None
        )
    return _auth_limiters[rpm]


def _auth_rate_limit(scope: str):
    """Dependency list throttling *scope*.

    The returned dependency resolves the shared limiter lazily on first request,
    so the decorator that consumes this list at import time does not read
    settings. When throttling is disabled the dependency is a no-op.
    """

    def dependency(request: Request) -> None:
        limiter = _get_auth_limiter()
        if limiter is None:
            return
        rate_limit(limiter, scope=scope)(request)

    return [Depends(dependency)]


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
    response_model=AccessTokenResponse,
    dependencies=_auth_rate_limit("login"),
)
def login(
    request: LoginRequest,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AccessTokenResponse:
    """Exchange email + password for an access token (+ refresh cookie).

    The refresh token is set as an httpOnly cookie rather than returned in the
    body, so it is never readable by page JavaScript.
    """
    try:
        pair = auth_service.login(request.email, request.password)
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
    _set_refresh_cookie(response, pair.refresh_token, settings)
    return _access_body(pair)


@router.post(
    "/refresh",
    response_model=AccessTokenResponse,
    dependencies=[*_auth_rate_limit("refresh"), Depends(verify_trusted_origin)],
)
def refresh(
    response: Response,
    body: RefreshRequest | None = None,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AccessTokenResponse:
    """Rotate the refresh token and return a fresh access token.

    The refresh token is read from the httpOnly cookie (browsers) or, as a
    fallback, the request body (non-browser clients). The rotated token is set
    as a new cookie; the body carries only the access token. A revoked or reused
    token is rejected with 401 and the stale cookie is cleared; reuse also
    revokes the user's whole token family server-side.
    """
    token = refresh_cookie or (body.refresh_token if body else None)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        pair = auth_service.refresh(token)
    except InvalidRefreshTokenError as exc:
        _clear_refresh_cookie(response, settings)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    _set_refresh_cookie(response, pair.refresh_token, settings)
    return _access_body(pair)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(verify_trusted_origin)],
)
def logout(
    response: Response,
    body: LogoutRequest | None = None,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Revoke the refresh token and clear its cookie.

    Idempotent — revoking an unknown or already-revoked token (or none at all)
    still returns 204 and clears the cookie. Access tokens already issued remain
    valid until they expire (they are stateless); keep their TTL short.
    """
    token = refresh_cookie or (body.refresh_token if body else None)
    if token:
        auth_service.logout(token)
    _clear_refresh_cookie(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserPublic)
def me(
    current_user: UserPublic = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> UserPublic:
    """Return the currently authenticated user.

    Resolves operator status the same way :func:`require_operator` does (stored
    ``is_operator`` flag OR ``operator_emails`` allow-list membership) so the
    flag the frontend uses to gate operator-only navigation matches what the
    backend actually authorizes. Without this, an allow-listed operator whose
    stored flag is unset would be authorized by operator endpoints while the UI
    hid the operator tabs.
    """
    if not current_user.is_operator and resolve_is_operator(current_user, settings):
        return current_user.model_copy(update={"is_operator": True})
    return current_user
