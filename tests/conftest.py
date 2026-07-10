"""Shared pytest fixtures.

Authentication was added to the API after these tests were written. The
pre-existing endpoint tests exercise document/query/trace behaviour and were
never about auth, so we override the ``get_current_user`` dependency with an
anonymous stand-in for the whole suite. This keeps those tests focused while the
application still defaults to auth-on in production.

Tests that specifically want to assert auth behaviour can clear or replace the
override on ``api_module.app.dependency_overrides`` within their own scope.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rag_system import api as api_module
from rag_system.auth.dependencies import get_current_user
from rag_system.auth.models import UserPublic

_TEST_USER = UserPublic(
    id="test-user",
    email="test@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    # Operator, so this fully-authorized stand-in sees the whole corpus and is
    # never owner-scoped — preserving the pre-auth behaviour these legacy
    # endpoint tests assume. Tests that assert non-operator/ownership behaviour
    # set their own user via a scoped dependency override.
    is_operator=True,
)


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Override the auth dependency so protected endpoints accept any request."""
    api_module.app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    try:
        yield
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Clear the in-process rate limiters between tests.

    The auth and SQL Lab endpoints use per-process sliding-window limiters keyed
    by client id. Under the TestClient every request shares one client key, so
    without a reset the accumulated hits from earlier tests could trip a 429 in a
    later, unrelated test (order-dependent flakiness). Clearing the caches gives
    each test a fresh limiter; no single test exceeds the allowance on its own.
    """
    import importlib

    auth_router_mod = importlib.import_module("rag_system.auth.router")
    sql_lab_router_mod = importlib.import_module("rag_system.sql_lab.router")
    auth_router_mod._auth_limiters.clear()
    sql_lab_router_mod._sql_lab_limiters.clear()
    yield
