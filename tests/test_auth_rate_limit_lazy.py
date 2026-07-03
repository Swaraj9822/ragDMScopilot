"""Regression tests for the lazily-initialized auth rate limiter.

The limiter used to be built at import time via ``get_settings()``, which made
module import (and thus test collection / app startup) fail unless the required
Settings fields were present in the environment. It is now resolved lazily on
first use, so these tests exercise that accessor directly.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from rag_system.rate_limit import SlidingWindowRateLimiter

# `rag_system.auth` re-exports the APIRouter as ``router``, which shadows the
# submodule name; import the module explicitly to reach its globals.
auth_router_mod = importlib.import_module("rag_system.auth.router")


def _reset_limiter(monkeypatch, rpm: int) -> None:
    monkeypatch.setattr(auth_router_mod, "_auth_limiter", None, raising=False)
    monkeypatch.setattr(auth_router_mod, "_auth_limiter_ready", False, raising=False)
    monkeypatch.setattr(
        auth_router_mod,
        "get_settings",
        lambda: SimpleNamespace(auth_rate_limit_per_minute=rpm),
    )


def test_limiter_built_lazily_when_enabled(monkeypatch) -> None:
    _reset_limiter(monkeypatch, rpm=5)
    limiter = auth_router_mod._get_auth_limiter()
    assert isinstance(limiter, SlidingWindowRateLimiter)
    # Cached: a second call returns the same instance without rebuilding.
    assert auth_router_mod._get_auth_limiter() is limiter


def test_limiter_disabled_when_zero(monkeypatch) -> None:
    _reset_limiter(monkeypatch, rpm=0)
    assert auth_router_mod._get_auth_limiter() is None


def test_rate_limit_dependency_list_has_single_dependency(monkeypatch) -> None:
    _reset_limiter(monkeypatch, rpm=5)
    deps = auth_router_mod._auth_rate_limit("login")
    assert len(deps) == 1
