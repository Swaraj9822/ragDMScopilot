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
)


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Override the auth dependency so protected endpoints accept any request."""
    api_module.app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    try:
        yield
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
