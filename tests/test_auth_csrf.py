"""CSRF protection for cookie-authenticated auth endpoints (finding #4/#6).

Two defenses, tested here:

* the refresh cookie defaults to ``SameSite=Lax`` (secure by default; cross-origin
  deployments opt into ``None`` explicitly);
* ``/auth/refresh`` and ``/auth/logout`` enforce an Origin/Referer check via
  :func:`verify_trusted_origin`, so a cross-site forgery that rides the httpOnly
  refresh cookie is rejected with ``403``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rag_system.auth.router import verify_trusted_origin
from rag_system.config import Settings

_SETTINGS = SimpleNamespace(
    cors_allow_origins_list=["https://console.example.com"]
)


def _request(headers: dict[str, str], netloc: str = "api.example.com"):
    return SimpleNamespace(
        headers=headers,
        url=SimpleNamespace(netloc=netloc),
    )


def test_samesite_defaults_to_lax() -> None:
    # Assert the class-level default (independent of any local .env override).
    assert Settings.model_fields["auth_cookie_samesite"].default == "lax"


def test_missing_origin_and_referer_is_allowed() -> None:
    # Non-browser clients (which use the body token, not the cookie) send no
    # Origin/Referer and are allowed; the cookie-CSRF vector requires a browser.
    verify_trusted_origin(_request({}), _SETTINGS)  # no raise


def test_same_origin_is_allowed() -> None:
    req = _request({"origin": "https://api.example.com"}, netloc="api.example.com")
    verify_trusted_origin(req, _SETTINGS)  # no raise


def test_configured_cors_origin_is_allowed() -> None:
    req = _request({"origin": "https://console.example.com"})
    verify_trusted_origin(req, _SETTINGS)  # no raise


def test_configured_origin_trailing_slash_normalized() -> None:
    req = _request({"origin": "https://console.example.com/"})
    verify_trusted_origin(req, _SETTINGS)  # no raise


def test_referer_fallback_when_origin_absent() -> None:
    req = _request({"referer": "https://console.example.com/app/page"})
    verify_trusted_origin(req, _SETTINGS)  # no raise


def test_foreign_origin_is_rejected() -> None:
    req = _request({"origin": "https://evil.example.net"})
    with pytest.raises(HTTPException) as exc:
        verify_trusted_origin(req, _SETTINGS)
    assert exc.value.status_code == 403
    assert exc.value.detail == "cross-origin request rejected"


def test_foreign_referer_is_rejected() -> None:
    req = _request({"referer": "https://evil.example.net/attack.html"})
    with pytest.raises(HTTPException) as exc:
        verify_trusted_origin(req, _SETTINGS)
    assert exc.value.status_code == 403
