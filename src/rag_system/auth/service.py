"""Authentication service: registration, login, and user lookup.

Coordinates the password hasher, the user store, and the token issuer. HTTP
concerns (status codes, headers) live in the router/dependencies; this layer
raises domain errors that those map to responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rag_system.auth import tokens
from rag_system.auth.models import TokenResponse, UserRecord
from rag_system.auth.passwords import hash_password, verify_password
from rag_system.auth.refresh_store import PostgresRefreshTokenStore
from rag_system.auth.store import EmailAlreadyExistsError, PostgresUserStore
from rag_system.config import Settings
from rag_system.observability import get_logger

logger = get_logger(__name__)

__all__ = [
    "AuthError",
    "EmailAlreadyExistsError",
    "InvalidCredentialsError",
    "InactiveUserError",
    "InvalidRefreshTokenError",
    "RegistrationClosedError",
    "AuthService",
]


class AuthError(Exception):
    """Base class for authentication failures."""


class InvalidCredentialsError(AuthError):
    """Raised when an email/password pair does not match an active account."""


class InactiveUserError(AuthError):
    """Raised when a known user exists but has been deactivated."""


class InvalidRefreshTokenError(AuthError):
    """Raised when a refresh token is unknown, expired, revoked, or reused."""


class RegistrationClosedError(AuthError):
    """Raised when self-service registration is disabled and an account exists."""


class AuthService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: PostgresUserStore | None = None,
        refresh_store: PostgresRefreshTokenStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or PostgresUserStore(settings)
        self._refresh_store = refresh_store or PostgresRefreshTokenStore(settings)

    def register(self, email: str, password: str) -> UserRecord:
        """Create a new account. Raises :class:`EmailAlreadyExistsError` on dupes.

        When self-service registration is disabled
        (``RAG_AUTH_ALLOW_REGISTRATION`` false), only the bootstrap account is
        permitted: once any user exists, further registration raises
        :class:`RegistrationClosedError`.
        """
        if not self._settings.auth_allow_registration and self._store.has_users():
            raise RegistrationClosedError(
                "Self-service registration is disabled."
            )
        record = self._store.create_user(email, hash_password(password))
        return record

    def authenticate(self, email: str, password: str) -> UserRecord:
        """Return the matching active user or raise.

        Always runs a hash verification — even when the user does not exist — so
        the response time does not reveal whether an email is registered
        (mitigates user-enumeration via timing).
        """
        user = self._store.get_by_email(email)
        stored_hash = user.password_hash if user else _DUMMY_HASH
        password_ok = verify_password(password, stored_hash)
        if user is None or not password_ok:
            raise InvalidCredentialsError("Incorrect email or password.")
        if not user.is_active:
            raise InactiveUserError("This account is disabled.")
        return user

    def login(self, email: str, password: str) -> TokenResponse:
        """Authenticate and issue a fresh access + refresh token pair."""
        user = self.authenticate(email, password)
        pair = self._issue_token_pair(user)
        logger.info("Issued token pair (login)", extra={"user_id": user.id})
        return pair

    def refresh(self, refresh_token: str) -> TokenResponse:
        """Rotate a refresh token, returning a new access + refresh pair.

        Implements rotation with reuse detection:

        * Unknown / expired token → :class:`InvalidRefreshTokenError`.
        * A token already revoked (e.g. one that was rotated out) being
          presented again is treated as theft: every refresh token for that
          user is revoked and the call fails. This invalidates both the
          attacker's and the victim's tokens, forcing a fresh login.
        * Otherwise the presented token is revoked and a brand-new pair issued.
        """
        token_hash = tokens.hash_refresh_token(refresh_token)
        record = self._refresh_store.get_by_hash(token_hash)
        if record is None:
            raise InvalidRefreshTokenError("Invalid refresh token.")

        if record.is_revoked:
            # Reuse of a rotated/revoked token — revoke the whole family.
            self._refresh_store.revoke_all_for_user(record.user_id)
            logger.warning(
                "Refresh token reuse detected; revoked all tokens for user",
                extra={"user_id": record.user_id},
            )
            raise InvalidRefreshTokenError("Refresh token has been revoked.")

        if record.is_expired():
            raise InvalidRefreshTokenError("Refresh token has expired.")

        user = self._store.get_by_id(record.user_id)
        if user is None or not user.is_active:
            raise InvalidRefreshTokenError("Account is unavailable.")

        # Rotate: revoke the presented token, then issue a new pair.
        self._refresh_store.revoke(record.id)
        pair = self._issue_token_pair(user)
        logger.info("Rotated refresh token", extra={"user_id": user.id})
        return pair

    def logout(self, refresh_token: str) -> None:
        """Revoke a refresh token. Idempotent: unknown tokens are a no-op."""
        token_hash = tokens.hash_refresh_token(refresh_token)
        record = self._refresh_store.get_by_hash(token_hash)
        if record is not None and not record.is_revoked:
            self._refresh_store.revoke(record.id)
            logger.info("Refresh token revoked (logout)", extra={"user_id": record.user_id})

    def get_user(self, user_id: str) -> UserRecord | None:
        return self._store.get_by_id(user_id)

    def _issue_token_pair(self, user: UserRecord) -> TokenResponse:
        """Mint a signed access token and persist a new opaque refresh token."""
        access = tokens.create_access_token(
            self._settings, subject=user.id, email=user.email
        )
        refresh_plain = tokens.generate_refresh_token()
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=self._settings.refresh_token_ttl_days
        )
        self._refresh_store.create(
            user.id, tokens.hash_refresh_token(refresh_plain), expires_at
        )
        return TokenResponse(
            access_token=access.token,
            refresh_token=refresh_plain,
            token_type=access.token_type,
            expires_in=access.expires_in,
        )


# A precomputed bcrypt hash of a random value, used as a constant-work stand-in
# when the email is unknown so authenticate() spends the same effort either way.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-comparison")
