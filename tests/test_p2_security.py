"""Tests for the P2 security-hardening changes.

Covers:
- Finding 6: an inbound X-Trace-Id is adopted only when syntactically valid.
- Finding 11: /metrics is token-gated when RAG_METRICS_TOKEN is set; / requires auth.
- Finding 8: /ask/stream streams to completion through the disconnect-aware loop.
- Finding 9: CopilotSqlGuard strips comments (string-literal aware) before validating.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth.dependencies import get_current_user
from rag_system.copilot import (
    CopilotColumn,
    CopilotSchemaCatalog,
    CopilotSqlGuard,
    CopilotTable,
    SqlValidationError,
)

_TRACE_ID_RE = api_module._TRACE_ID_RE


# ---------------------------------------------------------------------------
# Finding 6 — X-Trace-Id validation.
# ---------------------------------------------------------------------------


def test_valid_inbound_trace_id_is_preserved() -> None:
    client = TestClient(api_module.app)
    valid = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

    response = client.get("/health", headers={"X-Trace-Id": valid})

    assert response.headers["X-Trace-Id"] == valid


@pytest.mark.parametrize(
    "bad",
    [
        "A1B2C3D4E5F60718293A4B5C6D7E8F90",  # uppercase
        "not-a-valid-trace-id",
        "deadbeef",  # too short
        "g" * 32,  # non-hex
    ],
)
def test_malformed_inbound_trace_id_is_regenerated(bad: str) -> None:
    client = TestClient(api_module.app)

    response = client.get("/health", headers={"X-Trace-Id": bad})

    resolved = response.headers["X-Trace-Id"]
    assert resolved != bad
    assert _TRACE_ID_RE.fullmatch(resolved)


# ---------------------------------------------------------------------------
# Finding 11 — /metrics token gating and / auth.
# ---------------------------------------------------------------------------


def test_metrics_closed_by_default_when_auth_enabled_and_no_token(monkeypatch) -> None:
    # No dedicated metrics token + auth enabled → closed by default: an
    # anonymous scrape is refused rather than leaking metrics publicly.
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(metrics_token=None, auth_enabled=True),
    )
    client = TestClient(api_module.app)
    assert client.get("/metrics").status_code == 401


def test_metrics_open_when_auth_disabled_and_no_token(monkeypatch) -> None:
    # Trusted single-user/local deployment (auth disabled) keeps /metrics open.
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(metrics_token=None, auth_enabled=False),
    )
    client = TestClient(api_module.app)
    assert client.get("/metrics").status_code == 200


def test_metrics_requires_token_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        api_module, "get_settings", lambda: SimpleNamespace(metrics_token="s3cr3t")
    )
    client = TestClient(api_module.app)

    assert client.get("/metrics").status_code == 401
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cr3t"})
    assert ok.status_code == 200
    assert "rag_build_info" in ok.text


def test_root_endpoint_requires_authentication() -> None:
    def _raise_401():
        raise HTTPException(status_code=401, detail="Not authenticated.")

    api_module.app.dependency_overrides[get_current_user] = _raise_401
    try:
        client = TestClient(api_module.app)
        assert client.get("/").status_code == 401
        # A genuinely public endpoint is unaffected.
        assert client.get("/health").status_code == 200
    finally:
        # Restore the suite-wide anonymous bypass installed by conftest.
        api_module.app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Finding 8 — streaming completes through the disconnect-aware loop.
# ---------------------------------------------------------------------------


class _FakeRouter:
    def query_stream(self, request):
        yield {"type": "meta", "route": "rag"}
        yield {"type": "status", "stage": "generating"}
        yield {"type": "delta", "text": "Hello world"}
        yield {"type": "final", "response": {"answer": "Hello world"}}


def test_ask_stream_streams_to_completion(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "get_router", lambda: _FakeRouter())
    client = TestClient(api_module.app)

    response = client.post("/ask/stream", json={"question": "hi"})

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.text
    assert "Hello world" in body
    assert '"type": "final"' in body
    # A trace id is always surfaced for the client to correlate on.
    assert _TRACE_ID_RE.fullmatch(response.headers["X-Trace-Id"])


# ---------------------------------------------------------------------------
# Finding 9 — SQL guard comment stripping (string-literal aware).
# ---------------------------------------------------------------------------


def _guard() -> CopilotSqlGuard:
    catalog = CopilotSchemaCatalog(
        tables=[
            CopilotTable(
                name="orders",
                columns=[CopilotColumn(name="id"), CopilotColumn(name="total")],
            )
        ]
    )
    return CopilotSqlGuard(catalog, max_rows=100)


def test_line_comment_is_stripped_and_query_validates() -> None:
    guard = _guard()
    cleaned = guard.validate("select count(*) from orders -- ; drop table orders")
    assert "drop" not in cleaned.lower()
    assert "count(*)" in cleaned.lower()


def test_block_comment_hiding_write_keyword_does_not_false_reject() -> None:
    guard = _guard()
    cleaned = guard.validate("select count(*) /* delete update */ from orders")
    assert "delete" not in cleaned.lower()
    assert "orders" in cleaned.lower()


def test_string_literal_containing_comment_markers_is_preserved() -> None:
    guard = _guard()
    cleaned = guard.validate("select count(*) from orders where id = 'a--b/*x*/'")
    assert "'a--b/*x*/'" in cleaned


def test_real_second_statement_is_still_rejected() -> None:
    guard = _guard()
    with pytest.raises(SqlValidationError):
        guard.validate("select count(*) from orders; drop table orders")


def test_write_keyword_outside_comment_is_rejected() -> None:
    guard = _guard()
    with pytest.raises(SqlValidationError):
        guard.validate("select count(*) from orders where id in (delete from orders)")


# ---------------------------------------------------------------------------
# Finding 11 (extra) — startup warns when /metrics is open under auth.
# ---------------------------------------------------------------------------


def _lifespan_settings(*, metrics_token) -> SimpleNamespace:
    return SimpleNamespace(
        auth_enabled=True,
        tracing_enabled=False,
        metrics_token=metrics_token,
        require_jwt_secret=lambda: "secret",
    )


def _run_lifespan() -> None:
    import asyncio

    async def _run() -> None:
        async with api_module.lifespan(api_module.app):
            pass

    asyncio.run(_run())


def test_startup_warns_when_metrics_open_with_auth(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setattr(
        api_module, "get_settings", lambda: _lifespan_settings(metrics_token=None)
    )
    monkeypatch.setattr(api_module, "apply_auth_schema", lambda s: None)

    with caplog.at_level(logging.WARNING):
        _run_lifespan()

    assert any("RAG_METRICS_TOKEN is unset" in r.getMessage() for r in caplog.records)


def test_startup_no_warning_when_metrics_token_set(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setattr(
        api_module, "get_settings", lambda: _lifespan_settings(metrics_token="s3cr3t")
    )
    monkeypatch.setattr(api_module, "apply_auth_schema", lambda s: None)

    with caplog.at_level(logging.WARNING):
        _run_lifespan()

    assert not any(
        "RAG_METRICS_TOKEN is unset" in r.getMessage() for r in caplog.records
    )
