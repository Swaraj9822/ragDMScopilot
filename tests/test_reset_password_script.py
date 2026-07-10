"""Tests for the admin password-reset CLI (scripts/reset_password.py).

Covers the wiring — reset an existing account (and revoke its sessions), and a
clear failure for an unknown email — with the DB stores stubbed, so no live
database is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "reset_password",
    Path(__file__).resolve().parent.parent / "scripts" / "reset_password.py",
)
assert _SPEC is not None and _SPEC.loader is not None
reset_password = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reset_password)


class _FakeUserStore:
    def __init__(self, settings) -> None:
        self.calls: list[tuple[str, str]] = []
        self.record = SimpleNamespace(id="u-1", email="user@example.com")

    def set_password(self, email: str, password_hash: str):
        self.calls.append((email, password_hash))
        return self.record if email == "user@example.com" else None


class _FakeRefreshStore:
    revoked: list[str] = []
    raise_on_revoke: bool = False

    def __init__(self, settings) -> None:
        pass

    def revoke_all_for_user(self, user_id: str) -> None:
        if _FakeRefreshStore.raise_on_revoke:
            raise RuntimeError("db unreachable")
        _FakeRefreshStore.revoked.append(user_id)


def _patch(monkeypatch, user_store_cls):
    monkeypatch.setattr(reset_password, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(reset_password, "PostgresUserStore", user_store_cls)
    monkeypatch.setattr(reset_password, "PostgresRefreshTokenStore", _FakeRefreshStore)
    monkeypatch.setattr(reset_password, "hash_password", lambda pw: f"hash:{pw}")


def test_reset_existing_account_hashes_and_revokes_sessions(monkeypatch) -> None:
    _FakeRefreshStore.revoked = []
    _FakeRefreshStore.raise_on_revoke = False
    store = _FakeUserStore(None)
    _patch(monkeypatch, lambda settings: store)

    rc = reset_password.main(["user@example.com", "--password", "s3cret-password"])

    assert rc == 0
    assert store.calls == [("user@example.com", "hash:s3cret-password")]
    assert _FakeRefreshStore.revoked == ["u-1"]


def test_reset_returns_error_when_session_revocation_fails(monkeypatch) -> None:
    # The password change succeeds, but revocation fails: the script must NOT
    # claim success. It exits non-zero so an operator isn't falsely assured that
    # existing sessions were invalidated.
    _FakeRefreshStore.revoked = []
    _FakeRefreshStore.raise_on_revoke = True
    store = _FakeUserStore(None)
    _patch(monkeypatch, lambda settings: store)

    rc = reset_password.main(["user@example.com", "--password", "s3cret-password"])

    assert rc == 1
    assert store.calls == [("user@example.com", "hash:s3cret-password")]
    assert _FakeRefreshStore.revoked == []


def test_reset_unknown_account_returns_error(monkeypatch) -> None:
    _FakeRefreshStore.revoked = []
    _FakeRefreshStore.raise_on_revoke = False
    store = _FakeUserStore(None)
    _patch(monkeypatch, lambda settings: store)

    rc = reset_password.main(["ghost@example.com", "--password", "x-password"])

    assert rc == 1
    # No session revocation when there was no account to reset.
    assert _FakeRefreshStore.revoked == []
