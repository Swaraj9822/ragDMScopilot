"""Self-managed JWT authentication for the production-rag backend.

Public surface:

* :data:`router` — FastAPI router exposing ``/auth/register``, ``/auth/login``,
  and ``/auth/me``.
* :func:`get_current_user` — dependency that protects an endpoint with a bearer
  token and resolves the calling user.
* :func:`apply_schema` — idempotent ``users`` table setup, run at startup.
* :class:`AuthService` — registration / login / lookup orchestration.
"""

from __future__ import annotations

from rag_system.auth.dependencies import (
    get_auth_service,
    get_current_user,
    require_operator,
)
from rag_system.auth.models import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserPublic,
)
from rag_system.auth.router import router
from rag_system.auth.schema import apply_schema
from rag_system.auth.service import AuthService

__all__ = [
    "AuthService",
    "LoginRequest",
    "RegisterRequest",
    "TokenResponse",
    "UserPublic",
    "apply_schema",
    "get_auth_service",
    "get_current_user",
    "require_operator",
    "router",
]
