"""Tests for auth pydantic request/response models and UserRecord."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from rag_system.auth.models import (
    LoginRequest,
    RegisterRequest,
    UserRecord,
)


def test_register_normalizes_email_case_and_whitespace():
    req = RegisterRequest(email="  User@Example.COM  ", password="longenough")
    assert req.email == "user@example.com"


@pytest.mark.parametrize("bad_email", ["no-at-sign", "two@@x.com", "no-domain@dot", "a@b", "@x.com"])
def test_register_rejects_malformed_email(bad_email):
    with pytest.raises(ValidationError):
        RegisterRequest(email=bad_email, password="longenough")


def test_register_rejects_short_password():
    with pytest.raises(ValidationError):
        RegisterRequest(email="user@example.com", password="short")  # < 8 chars


def test_register_rejects_overly_long_password():
    with pytest.raises(ValidationError):
        RegisterRequest(email="user@example.com", password="x" * 257)


def test_login_lowercases_email_but_does_not_enforce_format():
    # A mistyped login should not raise a validation error — it should flow
    # through to become an "invalid credentials" failure instead.
    req = LoginRequest(email="  NoFormatCheck  ", password="x")
    assert req.email == "noformatcheck"


def test_user_record_to_public_drops_password_hash():
    record = UserRecord(
        id="abc",
        email="user@example.com",
        password_hash="$2b$secret",
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    public = record.to_public()
    assert public.id == "abc"
    assert public.email == "user@example.com"
    assert not hasattr(public, "password_hash")
    assert "secret" not in public.model_dump_json()


def test_user_record_defaults_to_non_operator():
    record = UserRecord(
        id="abc",
        email="user@example.com",
        password_hash="x",
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert record.is_operator is False
    assert record.to_public().is_operator is False


@pytest.mark.parametrize("is_operator", [True, False])
def test_user_record_to_public_copies_is_operator(is_operator):
    record = UserRecord(
        id="abc",
        email="user@example.com",
        password_hash="x",
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        is_operator=is_operator,
    )
    assert record.to_public().is_operator is is_operator
