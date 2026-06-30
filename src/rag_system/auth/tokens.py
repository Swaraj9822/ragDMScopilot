"""JWT access-token issuance and verification (self-managed, HS256).

Tokens are signed with the application's ``RAG_JWT_SECRET_KEY`` using the
configured algorithm (HS256 by default). The access token carries the user id
as the ``sub`` claim plus ``email`` for convenience, and standard ``iat``,
``exp``, ``iss``, and ``jti`` claims.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from rag_system.config import Settings

__all__ = [
    "TokenError",
    "AccessToken",
    "create_access_token",
    "decode_token",
    "generate_refresh_token",
    "hash_refresh_token",
]


class TokenError(Exception):
    """Raised when a token cannot be created or is invalid/expired."""


@dataclass(frozen=True)
class AccessToken:
    """A signed access token plus the metadata a client needs to use it."""

    token: str
    token_type: str
    expires_in: int  # seconds until expiry


def create_access_token(
    settings: Settings,
    *,
    subject: str,
    email: str,
    expires_delta: timedelta | None = None,
) -> AccessToken:
    """Create a signed access token for *subject* (the user id).

    The expiry defaults to ``settings.access_token_ttl_minutes``. Raises
    :class:`TokenError` if auth is misconfigured (no secret) or signing fails.
    """
    try:
        secret = settings.require_jwt_secret()
    except RuntimeError as exc:
        raise TokenError(str(exc)) from exc

    ttl = expires_delta or timedelta(minutes=settings.access_token_ttl_minutes)
    now = datetime.now(timezone.utc)
    expire = now + ttl
    claims = {
        "sub": subject,
        "email": email,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    try:
        token = jwt.encode(claims, secret, algorithm=settings.jwt_algorithm)
    except Exception as exc:  # noqa: BLE001 - normalise any signing failure
        raise TokenError(f"Failed to sign access token: {exc}") from exc

    # PyJWT >= 2 returns str; older versions returned bytes.
    if isinstance(token, bytes):  # pragma: no cover - defensive
        token = token.decode("ascii")
    return AccessToken(token=token, token_type="bearer", expires_in=int(ttl.total_seconds()))


def decode_token(settings: Settings, token: str) -> dict:
    """Verify and decode *token*, returning its claims.

    Validates the signature, expiry, and issuer. Raises :class:`TokenError`
    (never a raw PyJWT exception) when the token is expired, malformed, or
    otherwise invalid so callers map every failure to a single 401 path.
    """
    try:
        secret = settings.require_jwt_secret()
    except RuntimeError as exc:
        raise TokenError(str(exc)) from exc

    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid authentication token.") from exc


def generate_refresh_token() -> str:
    """Return a new opaque, high-entropy refresh token (URL-safe).

    The token itself is never stored — only its :func:`hash_refresh_token`
    digest — so a database disclosure cannot yield a usable refresh token.
    """
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Return the SHA-256 hex digest used to store/look up a refresh token.

    A plain (unsalted) SHA-256 is appropriate here because the input is a
    high-entropy random token, not a low-entropy password; lookups must be
    deterministic and there is nothing to brute-force.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
