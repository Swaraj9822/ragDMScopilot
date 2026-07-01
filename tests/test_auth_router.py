"""End-to-end HTTP tests for the /auth/* endpoints.

A fresh FastAPI app mounts the real auth router; the AuthService dependency is
backed by in-memory store doubles. Each test uses a distinct client IP (via
X-Forwarded-For) so the router's per-process rate limiter cannot leak state
between tests.
"""

from __future__ import annotations

import itertools

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_system.auth import dependencies as deps
from rag_system.auth.router import router as auth_router
from rag_system.auth.service import AuthService
from rag_system.config import get_settings

from auth_doubles import InMemoryRefreshStore, InMemoryUserStore, make_settings

_ip_counter = itertools.count(1)


def _build_client(**setting_overrides) -> TestClient:
    settings = make_settings(**setting_overrides)
    service = AuthService(
        settings, store=InMemoryUserStore(), refresh_store=InMemoryRefreshStore()
    )
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[deps.get_auth_service] = lambda: service
    app.dependency_overrides[get_settings] = lambda: settings
    # Unique source IP per client keeps rate-limit buckets isolated per test.
    unique_ip = f"10.0.0.{next(_ip_counter)}"
    return TestClient(app, headers={"X-Forwarded-For": unique_ip})


def _register(client, email="user@example.com", password="password123"):
    return client.post("/auth/register", json={"email": email, "password": password})


def _login(client, email="user@example.com", password="password123"):
    return client.post("/auth/login", json={"email": email, "password": password})


# --- register --------------------------------------------------------------


def test_register_returns_201_and_public_user():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    resp = _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "user@example.com"
    assert "password_hash" not in body
    assert "password" not in body


def test_register_duplicate_returns_409():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    _register(client)
    resp = _register(client)
    assert resp.status_code == 409


def test_register_closed_returns_403():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=False)
    assert _register(client, email="first@example.com").status_code == 201  # bootstrap
    resp = _register(client, email="second@example.com")
    assert resp.status_code == 403


def test_register_invalid_email_returns_422():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    resp = client.post("/auth/register", json={"email": "bogus", "password": "password123"})
    assert resp.status_code == 422


# --- login -----------------------------------------------------------------


def test_login_returns_token_pair():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    _register(client)
    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


def test_login_wrong_password_returns_401():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    _register(client)
    resp = _login(client, password="wrong")
    assert resp.status_code == 401


# --- refresh / logout ------------------------------------------------------


def test_refresh_returns_new_pair():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    _register(client)
    refresh_token = _login(client).json()["refresh_token"]
    resp = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] != refresh_token


def test_refresh_invalid_token_returns_401():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    resp = client.post("/auth/refresh", json={"refresh_token": "nope"})
    assert resp.status_code == 401


def test_logout_returns_204_and_invalidates_refresh():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    _register(client)
    refresh_token = _login(client).json()["refresh_token"]

    assert client.post("/auth/logout", json={"refresh_token": refresh_token}).status_code == 204
    # Once logged out the refresh token no longer works.
    assert client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401


# --- protected /auth/me ----------------------------------------------------


def test_me_requires_authentication():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    assert client.get("/auth/me").status_code == 401


def test_me_returns_current_user_with_valid_token():
    client = _build_client(RAG_AUTH_ALLOW_REGISTRATION=True)
    _register(client)
    access = _login(client).json()["access_token"]
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "user@example.com"
