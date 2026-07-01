"""Tests for password hashing (bcrypt + SHA-256 pre-hash)."""

from __future__ import annotations

import pytest

from rag_system.auth.passwords import hash_password, verify_password


def test_hash_is_not_plaintext_and_verifies():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed) is True


def test_wrong_password_does_not_verify():
    hashed = hash_password("s3cret-pass")
    assert verify_password("not-the-password", hashed) is False


def test_same_password_produces_different_hashes():
    """Distinct salts mean two hashes of the same password differ, yet both verify."""
    a = hash_password("repeatable")
    b = hash_password("repeatable")
    assert a != b
    assert verify_password("repeatable", a)
    assert verify_password("repeatable", b)


def test_password_longer_than_bcrypt_72_byte_limit_is_not_truncated():
    """The SHA-256 pre-hash must make >72-byte passwords fully significant.

    Two long passwords that share the first 72 bytes but differ afterwards must
    NOT be interchangeable — that is the bug the pre-hash exists to prevent.
    """
    base = "A" * 72
    pw1 = base + "-tail-one"
    pw2 = base + "-tail-two"
    hashed = hash_password(pw1)
    assert verify_password(pw1, hashed) is True
    assert verify_password(pw2, hashed) is False


def test_embedded_null_byte_does_not_truncate():
    hashed = hash_password("abc\x00def")
    assert verify_password("abc\x00def", hashed) is True
    assert verify_password("abc", hashed) is False


def test_unicode_password_roundtrips():
    pw = "pässwörd-🔐-山田"
    assert verify_password(pw, hash_password(pw)) is True


@pytest.mark.parametrize("bad", ["", "not-a-bcrypt-hash", "$2b$xx$broken", "12345"])
def test_verify_against_malformed_hash_returns_false_not_raises(bad):
    assert verify_password("anything", bad) is False
