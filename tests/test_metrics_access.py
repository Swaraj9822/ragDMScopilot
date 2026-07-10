"""Access control for the /metrics endpoint (finding #8).

``/metrics`` exposes route latencies, error rates, and model ids. It must not be
publicly readable when auth is enabled: a dedicated ``RAG_METRICS_TOKEN`` gates
the scraper path, and absent that token the endpoint requires a valid user token
(closed by default) rather than being open.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rag_system import api as api_module
from rag_system.api import verify_metrics_access


def _request(authorization: str | None = None):
    headers = {"Authorization": authorization} if authorization is not None else {}
    return SimpleNamespace(headers=headers)


@pytest.fixture
def settings(monkeypatch):
    def _apply(**kwargs):
        base = {"metrics_token": None, "auth_enabled": True}
        base.update(kwargs)
        cfg = SimpleNamespace(**base)
        monkeypatch.setattr(api_module, "get_settings", lambda: cfg)
        return cfg

    return _apply


def test_token_configured_requires_matching_bearer(settings) -> None:
    settings(metrics_token="scrape-secret")
    with pytest.raises(HTTPException) as exc:
        verify_metrics_access(_request("Bearer wrong"))
    assert exc.value.status_code == 401


def test_token_configured_accepts_matching_bearer(settings) -> None:
    settings(metrics_token="scrape-secret")
    verify_metrics_access(_request("Bearer scrape-secret"))  # no raise


def test_no_token_auth_disabled_is_open(settings) -> None:
    settings(metrics_token=None, auth_enabled=False)
    verify_metrics_access(_request())  # no raise


def test_no_token_auth_enabled_rejects_anonymous(settings) -> None:
    # The security fix: closed by default when auth is on and no scrape token is
    # configured — an anonymous (no-bearer) request is refused rather than served.
    settings(metrics_token=None, auth_enabled=True)
    with pytest.raises(HTTPException) as exc:
        verify_metrics_access(_request())
    assert exc.value.status_code == 401
