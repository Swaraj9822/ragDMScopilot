"""Tests for JWT access-token issuance/verification and refresh-token helpers."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from rag_system.auth.tokens import (
    AccessToken,
    TokenError,
    create_access_token,
    decode_token,
    generate_refresh_token,
    hash_refresh_token,
)

from auth_doubles import make_settings


def test_create_and_decode_roundtrip():
    settings = make_settings()
    issued = create_access_token(settings, subject="user-123", email="u@example.com")
    assert isinstance(issued, AccessToken)
    assert issued.token_type == "bearer"
    assert issued.expires_in == settings.access_token_ttl_minutes * 60

    claims = decode_token(settings, issued.token)
    assert claims["sub"] == "user-123"
    assert claims["email"] == "u@example.com"
    assert claims["iss"] == settings.jwt_issuer
    assert "exp" in claims and "iat" in claims and "jti" in claims


def test_each_token_has_a_unique_jti():
    settings = make_settings()
    t1 = create_access_token(settings, subject="u", email="u@x.com")
    t2 = create_access_token(settings, subject="u", email="u@x.com")
    assert decode_token(settings, t1.token)["jti"] != decode_token(settings, t2.token)["jti"]


def test_expired_token_raises_tokenerror():
    settings = make_settings()
    expired = create_access_token(
        settings, subject="u", email="u@x.com", expires_delta=timedelta(seconds=-1)
    )
    with pytest.raises(TokenError):
        decode_token(settings, expired.token)


def test_tampered_signature_raises_tokenerror():
    settings = make_settings()
    token = create_access_token(settings, subject="u", email="u@x.com").token
    tampered = token[:-3] + ("aaa" if token[-3:] != "aaa" else "bbb")
    with pytest.raises(TokenError):
        decode_token(settings, tampered)


def test_token_signed_with_other_secret_is_rejected():
    signer = make_settings(RAG_JWT_SECRET_KEY="secret-A")
    verifier = make_settings(RAG_JWT_SECRET_KEY="secret-B")
    token = create_access_token(signer, subject="u", email="u@x.com").token
    with pytest.raises(TokenError):
        decode_token(verifier, token)


def test_wrong_issuer_is_rejected():
    signer = make_settings(RAG_JWT_ISSUER="attacker")
    verifier = make_settings(RAG_JWT_ISSUER="production-rag-tests")
    token = create_access_token(signer, subject="u", email="u@x.com").token
    with pytest.raises(TokenError):
        decode_token(verifier, token)


def test_garbage_string_raises_tokenerror():
    settings = make_settings()
    with pytest.raises(TokenError):
        decode_token(settings, "not.a.jwt")


def test_missing_secret_raises_tokenerror_on_create_and_decode():
    settings = make_settings(RAG_JWT_SECRET_KEY=None)
    with pytest.raises(TokenError):
        create_access_token(settings, subject="u", email="u@x.com")
    # A well-formed token from elsewhere still cannot be decoded without a secret.
    good = create_access_token(make_settings(), subject="u", email="u@x.com").token
    with pytest.raises(TokenError):
        decode_token(settings, good)


def test_token_missing_required_claim_is_rejected():
    """A token lacking a required claim (sub) must fail decode_token."""
    settings = make_settings()
    secret = settings.require_jwt_secret()
    partial = jwt.encode(
        {"iss": settings.jwt_issuer, "exp": 9999999999},
        secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(settings, partial)


def test_generate_refresh_token_is_high_entropy_and_unique():
    tokens_seen = {generate_refresh_token() for _ in range(200)}
    assert len(tokens_seen) == 200
    assert all(len(t) >= 43 for t in tokens_seen)  # token_urlsafe(48) -> 64 chars


def test_hash_refresh_token_is_deterministic_and_hides_input():
    token = generate_refresh_token()
    digest = hash_refresh_token(token)
    assert digest == hash_refresh_token(token)
    assert token not in digest
    assert len(digest) == 64  # sha256 hex
    assert hash_refresh_token("a") != hash_refresh_token("b")
