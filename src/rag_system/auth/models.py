"""Pydantic request/response models and the internal user record for auth.

A pragmatic email regex is used instead of :class:`pydantic.EmailStr` so the
package does not require the optional ``email-validator`` dependency. The check
is intentionally permissive (one ``@``, a dot in the domain) — it guards against
obvious mistakes, not RFC-perfect validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "RefreshRequest",
    "LogoutRequest",
    "TokenResponse",
    "UserPublic",
    "UserRecord",
]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# bcrypt-safe password bounds. The SHA-256 pre-hash removes bcrypt's 72-byte
# limit, but a sane upper bound still prevents abusive megabyte-long inputs.
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 256


def _normalize_email(value: str) -> str:
    value = value.strip()
    if not _EMAIL_RE.match(value):
        raise ValueError("Invalid email address.")
    return value.lower()


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=_MIN_PASSWORD_LEN, max_length=_MAX_PASSWORD_LEN)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _normalize_email(value)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=_MAX_PASSWORD_LEN)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        # Login normalises case but does not enforce format/length rules so a
        # mistyped login fails as "invalid credentials", not a validation error.
        return value.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the access token expires


class UserPublic(BaseModel):
    """User fields safe to return over the API (never the password hash)."""

    id: str
    email: str
    is_active: bool
    created_at: datetime


@dataclass(frozen=True)
class UserRecord:
    """Full user row as stored, including the password hash (server-side only)."""

    id: str
    email: str
    password_hash: str
    is_active: bool
    created_at: datetime

    def to_public(self) -> UserPublic:
        return UserPublic(
            id=self.id,
            email=self.email,
            is_active=self.is_active,
            created_at=self.created_at,
        )
