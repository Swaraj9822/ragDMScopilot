"""Tests for wiring the AI configuration resolver onto query traces (R9.1, 15.6).

Regression: ``AIConfigResolver`` was never invoked on the answer path, so every
trace stamped the ``unresolved`` sentinel. ``api._stamp_trace_config`` now
resolves the active configuration version and records it (plus redacted
settings) on the trace's root span.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from rag_system import api as api_module
from rag_system.ai_config import UNRESOLVED_VERSION_ID
from rag_system.storage import PreconditionFailed


class _FakeStore:
    """Minimal in-memory JSON store with create-only + CAS semantics."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def create_json(self, key: str, payload: object) -> str:
        if key in self.objects:
            raise PreconditionFailed(key)
        self.objects[key] = payload
        return f"s3://bucket/{key}"

    def update_json_cas(
        self,
        key: str,
        mutate: Callable[[object | None], object],
        *,
        max_attempts: int = 5,
    ) -> object:
        result = mutate(self.objects.get(key))
        self.objects[key] = result
        return result


class _RecordingRecorder:
    """Captures the arguments passed to set_trace_config."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def set_trace_config(
        self,
        span: object,
        *,
        ai_configuration_version_id: str | None,
        resolved_settings: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(
            {
                "span": span,
                "ai_configuration_version_id": ai_configuration_version_id,
                "resolved_settings": resolved_settings,
            }
        )


@pytest.fixture
def stamping_env(monkeypatch):
    store = _FakeStore()
    recorder = _RecordingRecorder()
    monkeypatch.setattr(api_module, "_get_artifact_store", lambda: store)
    monkeypatch.setattr(api_module, "get_span_recorder", lambda: recorder)
    return store, recorder


def test_stamp_trace_config_records_a_resolved_version(stamping_env):
    """On a fresh store the resolver bootstraps a default version and stamps it
    (never the ``unresolved`` sentinel)."""
    _store, recorder = stamping_env
    sentinel_span = object()

    api_module._stamp_trace_config(sentinel_span)

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["span"] is sentinel_span
    assert call["ai_configuration_version_id"] != UNRESOLVED_VERSION_ID
    assert call["ai_configuration_version_id"]  # non-empty
    assert isinstance(call["resolved_settings"], dict)


def test_stamp_trace_config_redacts_secrets(stamping_env):
    """Resolved settings recorded on the trace never expose secret values."""
    _store, recorder = stamping_env

    api_module._stamp_trace_config(object())

    settings = recorder.calls[0]["resolved_settings"]
    # The bootstrapped default carries retrieval/reranker settings; ensure no
    # obviously-secret key leaks a raw value (the payload builder redacts).
    flat = str(settings).lower()
    assert "secret" not in flat or "***" in flat


def test_stamp_trace_config_never_raises(monkeypatch):
    """A store/resolver failure degrades to a logged warning, not an exception."""

    def _boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(api_module, "_get_artifact_store", _boom)
    # Should not raise even though the store construction blows up.
    api_module._stamp_trace_config(object())
