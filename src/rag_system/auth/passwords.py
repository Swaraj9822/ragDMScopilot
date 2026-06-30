"""Password hashing for self-managed authentication.

Uses ``bcrypt`` for the actual key-derivation, with a SHA-256 pre-hash so that
passwords longer than bcrypt's 72-byte input limit are not silently truncated
(and embedded NUL bytes cannot terminate the input early). The pre-hash digest
is base64-encoded (44 bytes) which is comfortably within bcrypt's limit.

Only this module knows how a password is stored; the rest of the auth package
treats the result as an opaque hash string.
"""

from __future__ import annotations

import base64
import hashlib

import bcrypt

__all__ = ["hash_password", "verify_password"]


def _prehash(password: str) -> bytes:
    """Return a fixed-length, bcrypt-safe representation of *password*.

    SHA-256 yields 32 bytes regardless of input length; base64 encoding makes it
    44 ASCII bytes (< 72) with no NUL bytes, so bcrypt consumes the whole value.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Hash a plaintext password, returning a self-describing bcrypt hash string."""
    hashed = bcrypt.hashpw(_prehash(password), bcrypt.gensalt())
    return hashed.decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Return ``True`` iff *password* matches the stored bcrypt *hashed* string.

    Never raises: a malformed stored hash is treated as a non-match so callers
    can rely on a boolean result for the credential check.
    """
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
