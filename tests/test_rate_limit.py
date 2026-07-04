"""Tests for the sliding-window rate limiter and its FastAPI dependency."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import rag_system.rate_limit as rate_limit_module
from rag_system.rate_limit import SlidingWindowRateLimiter, rate_limit


def _fake_request(*, xff: str | None = None, host: str = "203.0.113.7", path: str = "/auth/login"):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=host),
        url=SimpleNamespace(path=path),
    )


# --- limiter core ----------------------------------------------------------


def test_limit_must_be_at_least_one():
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(limit=0, window_seconds=60)


def test_allows_up_to_limit_then_blocks():
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
    assert limiter.check("k") == (True, 0)
    assert limiter.check("k") == (True, 0)
    allowed, retry_after = limiter.check("k")
    assert allowed is False
    assert retry_after > 0


def test_keys_are_independent():
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("a")[0] is True
    assert limiter.check("b")[0] is True  # different key, own budget
    assert limiter.check("a")[0] is False


def test_events_age_out_of_the_window(monkeypatch):
    """Once the window elapses, earlier events no longer count."""
    now = {"t": 1000.0}
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: now["t"])

    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=10)
    assert limiter.check("k")[0] is True
    assert limiter.check("k")[0] is False  # still within window

    now["t"] += 11  # advance past the window
    assert limiter.check("k")[0] is True  # earlier event has aged out


def test_idle_one_off_keys_are_pruned(monkeypatch):
    """One-off keys must not accumulate forever (memory-leak regression).

    Simulates an attacker cycling spoofed X-Forwarded-For values: each key is
    seen once, then never again. Their fully-aged buckets must be swept so
    ``_hits`` tracks the active key set rather than every key ever observed.
    """
    now = {"t": 1000.0}
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: now["t"])

    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=10)

    # 100 distinct one-off keys within the first window.
    for i in range(100):
        assert limiter.check(f"ip-{i}")[0] is True
    assert len(limiter._hits) == 100

    # Advance past the window and touch a single new key; the sweep should
    # evict the 100 now-fully-aged keys, leaving only the active one.
    now["t"] += 11
    assert limiter.check("ip-live")[0] is True
    assert set(limiter._hits) == {"ip-live"}


def test_active_key_survives_pruning(monkeypatch):
    """A key still receiving in-window hits is never pruned."""
    now = {"t": 1000.0}
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: now["t"])

    limiter = SlidingWindowRateLimiter(limit=5, window_seconds=10)

    # Keep "hot" active across several sweeps; "cold" is touched once and ages out.
    assert limiter.check("cold")[0] is True
    for _ in range(3):
        assert limiter.check("hot")[0] is True
        now["t"] += 11  # cross a window boundary each iteration -> triggers sweep
    assert "hot" in limiter._hits
    assert "cold" not in limiter._hits


# --- dependency ------------------------------------------------------------


def test_dependency_allows_within_limit():
    limiter = SlidingWindowRateLimiter(limit=5, window_seconds=60)
    dep = rate_limit(limiter, scope="login")
    # Should not raise for the first request.
    assert dep(_fake_request()) is None


def test_dependency_raises_429_with_retry_after_header():
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    dep = rate_limit(limiter, scope="login")
    req = _fake_request()
    dep(req)  # consume the single allowed slot
    with pytest.raises(HTTPException) as exc:
        dep(req)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_dependency_buckets_by_scope_and_client():
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    login_dep = rate_limit(limiter, scope="login")
    register_dep = rate_limit(limiter, scope="register")
    req = _fake_request()
    # Same client, different scope -> independent buckets.
    assert login_dep(req) is None
    assert register_dep(req) is None
    # Second hit on the same scope is blocked.
    with pytest.raises(HTTPException):
        login_dep(req)


def test_forwarded_for_takes_precedence_over_peer_address():
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    dep = rate_limit(limiter, scope="login")
    # Two requests from the same peer host but different XFF should not collide.
    dep(_fake_request(xff="1.1.1.1", host="10.0.0.1"))
    dep(_fake_request(xff="2.2.2.2", host="10.0.0.1"))  # different XFF -> own bucket
    with pytest.raises(HTTPException):
        dep(_fake_request(xff="1.1.1.1", host="10.0.0.1"))  # repeat of first XFF
