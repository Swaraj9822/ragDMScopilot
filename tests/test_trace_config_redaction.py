"""Tests for configuration redaction and trace recording integration (R9.1, R9.2, R9.11).

Covers:
- redact_settings deep-copies and never mutates the source.
- Sensitive keys (api_key, secret, token, credential, password) are replaced
  with the redaction placeholder at the top level and in nested dicts.
- Non-sensitive keys are preserved unchanged.
- Case-insensitive matching of sensitive patterns.
- build_trace_config_payload produces a correct payload for resolved configs.
- build_trace_config_payload produces an unresolved payload when is_resolved=False.
- SpanRecorder.set_trace_config stamps config version and settings on the span.
- group_spans_by_trace propagates config version to the assembled Trace.
- Serializer round-trips the config version and resolved settings.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone


from rag_system.ai_config import UNRESOLVED_VERSION_ID, ResolvedConfig
from rag_system.observability_tracing.config_redaction import (
    REDACTED_PLACEHOLDER,
    build_trace_config_payload,
    redact_settings,
)
from rag_system.observability_tracing.flush_workers import group_spans_by_trace
from rag_system.observability_tracing.models import Span, Trace
from rag_system.observability_tracing.serializer import TraceSerializer


# ---------------------------------------------------------------------------
# redact_settings
# ---------------------------------------------------------------------------


class TestRedactSettings:
    """Tests for the redact_settings utility."""

    def test_does_not_mutate_source(self) -> None:
        """redact_settings must never mutate the source dict."""
        source = {
            "model": "gemini-3.5-flash",
            "api_key": "sk-secret-123",
            "retrieval_settings": {
                "token_budget": 100,
                "provider_secret": "s3cr3t",
            },
        }
        original = copy.deepcopy(source)
        redact_settings(source)
        assert source == original

    def test_redacts_top_level_sensitive_keys(self) -> None:
        """All defined sensitive patterns are redacted at the top level."""
        source = {
            "my_api_key": "key-value",
            "db_secret": "very-secret",
            "auth_token": "tok-123",
            "service_credential": "cred-abc",
            "admin_password": "pass-xyz",
            "model": "gemini-3.5-flash",
        }
        result = redact_settings(source)
        assert result["my_api_key"] == REDACTED_PLACEHOLDER
        assert result["db_secret"] == REDACTED_PLACEHOLDER
        assert result["auth_token"] == REDACTED_PLACEHOLDER
        assert result["service_credential"] == REDACTED_PLACEHOLDER
        assert result["admin_password"] == REDACTED_PLACEHOLDER
        # Non-sensitive preserved
        assert result["model"] == "gemini-3.5-flash"

    def test_redacts_nested_sensitive_keys(self) -> None:
        """Sensitive keys nested inside settings sub-dicts are redacted."""
        source = {
            "prompt": "answer questions",
            "retrieval_settings": {
                "retrieval_dense_top_k": 20,
                "provider_api_key": "real-key",
                "nested": {
                    "deep_secret": "hidden",
                    "count": 5,
                },
            },
            "extra_settings": {
                "context_top_k": 10,
                "auth_token": "extra-token",
            },
        }
        result = redact_settings(source)
        assert result["prompt"] == "answer questions"
        assert result["retrieval_settings"]["retrieval_dense_top_k"] == 20
        assert result["retrieval_settings"]["provider_api_key"] == REDACTED_PLACEHOLDER
        assert result["retrieval_settings"]["nested"]["deep_secret"] == REDACTED_PLACEHOLDER
        assert result["retrieval_settings"]["nested"]["count"] == 5
        assert result["extra_settings"]["context_top_k"] == 10
        assert result["extra_settings"]["auth_token"] == REDACTED_PLACEHOLDER

    def test_case_insensitive_matching(self) -> None:
        """Pattern matching is case-insensitive."""
        source = {
            "API_KEY": "upper",
            "Secret": "title",
            "TOKEN_VALUE": "mixed",
            "My_Credential": "cred",
            "PASSWORD_hash": "pw",
        }
        result = redact_settings(source)
        for key in source:
            assert result[key] == REDACTED_PLACEHOLDER

    def test_empty_dict_returns_empty(self) -> None:
        """An empty settings dict returns an empty dict."""
        assert redact_settings({}) == {}

    def test_non_sensitive_keys_preserved(self) -> None:
        """Keys that don't match any pattern are preserved with their original values."""
        source = {
            "prompt": "hello",
            "model": "gemini",
            "router_threshold": 0.7,
            "output_schema": {"type": "object"},
        }
        result = redact_settings(source)
        assert result == source
        # Verify it's a copy, not the same object
        assert result is not source


# ---------------------------------------------------------------------------
# build_trace_config_payload
# ---------------------------------------------------------------------------


class TestBuildTraceConfigPayload:
    """Tests for build_trace_config_payload."""

    def test_resolved_config_produces_redacted_payload(self) -> None:
        """A resolved config produces a payload with version_id and redacted settings."""
        rc = ResolvedConfig(
            version_id="v-123",
            config_id="default",
            prompt="answer questions",
            model="gemini-3.5-flash",
            output_schema={"type": "object"},
            router_threshold=0.5,
            retrieval_settings={"top_k": 20, "provider_api_key": "real-key"},
            is_resolved=True,
        )
        payload = build_trace_config_payload(rc)

        assert payload["ai_configuration_version_id"] == "v-123"
        settings = payload["resolved_settings"]
        assert settings["prompt"] == "answer questions"
        assert settings["model"] == "gemini-3.5-flash"
        assert settings["output_schema"] == {"type": "object"}
        assert settings["router_threshold"] == 0.5
        assert settings["retrieval_settings"]["top_k"] == 20
        assert settings["retrieval_settings"]["provider_api_key"] == REDACTED_PLACEHOLDER

    def test_resolved_config_source_not_mutated(self) -> None:
        """build_trace_config_payload never mutates the source ResolvedConfig."""
        rc = ResolvedConfig(
            version_id="v-456",
            config_id="default",
            prompt="p",
            model="m",
            output_schema={},
            router_threshold=0.5,
            retrieval_settings={"api_key_value": "original"},
            is_resolved=True,
        )
        build_trace_config_payload(rc)
        # Source settings still contain the original value
        assert rc.retrieval_settings["api_key_value"] == "original"

    def test_unresolved_config_produces_unresolved_payload(self) -> None:
        """An unresolved config produces a payload with the unresolved sentinel."""
        rc = ResolvedConfig.unresolved("my-cfg")
        payload = build_trace_config_payload(rc)

        assert payload["ai_configuration_version_id"] == UNRESOLVED_VERSION_ID
        assert payload["resolved_settings"] == {}


# ---------------------------------------------------------------------------
# SpanRecorder.set_trace_config integration
# ---------------------------------------------------------------------------


class TestSetTraceConfig:
    """Tests for SpanRecorder.set_trace_config."""

    def _make_recorder(self):
        from rag_system.observability_tracing.recorder import SpanRecorder

        class _FakeSampler:
            def should_record(self, **kwargs):
                return True

        return SpanRecorder(sampler=_FakeSampler())

    def test_stamps_version_id_on_span(self) -> None:
        """set_trace_config records the version id as a span attribute."""
        recorder = self._make_recorder()
        span = Span(
            span_id="s1",
            parent_span_id=None,
            operation="Root_Span",
            start_ts=datetime.now(timezone.utc),
            duration_ms=0,
            status="success",
        )
        recorder.set_trace_config(
            span,
            ai_configuration_version_id="v-test",
            resolved_settings={"prompt": "hello"},
        )
        assert span.attributes["ai_configuration_version_id"] == "v-test"
        assert span._trace_config_version_id == "v-test"  # type: ignore[attr-defined]
        assert span._trace_resolved_settings == {"prompt": "hello"}  # type: ignore[attr-defined]

    def test_stamps_unresolved_when_none(self) -> None:
        """set_trace_config records 'unresolved' when version_id is None."""
        recorder = self._make_recorder()
        span = Span(
            span_id="s2",
            parent_span_id=None,
            operation="Root_Span",
            start_ts=datetime.now(timezone.utc),
            duration_ms=0,
            status="success",
        )
        recorder.set_trace_config(
            span,
            ai_configuration_version_id=None,
            resolved_settings=None,
        )
        assert span.attributes["ai_configuration_version_id"] == "unresolved"
        assert span._trace_config_version_id is None  # type: ignore[attr-defined]
        assert span._trace_resolved_settings == {}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# group_spans_by_trace propagation
# ---------------------------------------------------------------------------


class TestGroupSpansByTraceConfigPropagation:
    """Tests that group_spans_by_trace propagates config version to Trace."""

    def test_propagates_config_from_root_span(self) -> None:
        """Config version on the root span is propagated to the assembled Trace."""
        root = Span(
            span_id="root",
            parent_span_id=None,
            operation="Root_Span",
            start_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            duration_ms=100,
            status="success",
            trace_id="trace-1",
            route="rag",
        )
        root._trace_config_version_id = "v-propagated"  # type: ignore[attr-defined]
        root._trace_resolved_settings = {"model": "gemini"}  # type: ignore[attr-defined]

        child = Span(
            span_id="child",
            parent_span_id="root",
            operation="retrieval",
            start_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            duration_ms=50,
            status="success",
            trace_id="trace-1",
        )

        traces = group_spans_by_trace([root, child])
        assert len(traces) == 1
        assert traces[0].ai_configuration_version_id == "v-propagated"
        assert traces[0].resolved_settings == {"model": "gemini"}

    def test_no_config_on_span_leaves_trace_fields_none_and_empty(self) -> None:
        """When no config is stamped, the trace gets None and empty dict."""
        span = Span(
            span_id="s1",
            parent_span_id=None,
            operation="Root_Span",
            start_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            duration_ms=100,
            status="success",
            trace_id="trace-2",
            route="rag",
        )
        traces = group_spans_by_trace([span])
        assert len(traces) == 1
        assert traces[0].ai_configuration_version_id is None
        assert traces[0].resolved_settings == {}


# ---------------------------------------------------------------------------
# Serializer round-trip with config version
# ---------------------------------------------------------------------------


class TestSerializerConfigRoundTrip:
    """Tests that the serializer round-trips AI config version and settings."""

    def test_serialize_includes_config_version(self) -> None:
        """serialize includes ai_configuration_version_id and resolved_settings."""
        trace = Trace(
            trace_id="t1",
            route="rag",
            start_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            duration_ms=100,
            root_status="success",
            spans=[],
            ai_configuration_version_id="v-ser",
            resolved_settings={"model": "gemini", "prompt": "test"},
        )
        serializer = TraceSerializer()
        stored = serializer.serialize(trace)
        assert stored["ai_configuration_version_id"] == "v-ser"
        assert stored["resolved_settings"] == {"model": "gemini", "prompt": "test"}

    def test_serialize_omits_config_when_none(self) -> None:
        """serialize omits ai_configuration_version_id when it's None."""
        trace = Trace(
            trace_id="t2",
            route="rag",
            start_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            duration_ms=100,
            root_status="success",
            spans=[],
        )
        serializer = TraceSerializer()
        stored = serializer.serialize(trace)
        assert "ai_configuration_version_id" not in stored

    def test_deserialize_restores_config_version(self) -> None:
        """deserialize restores ai_configuration_version_id and resolved_settings."""
        stored = {
            "trace_id": "t3",
            "route": "rag",
            "start_ts": "2024-01-01T00:00:00+00:00",
            "duration_ms": 50,
            "root_status": "success",
            "spans": [],
            "ai_configuration_version_id": "v-deser",
            "resolved_settings": {"router_threshold": 0.7},
        }
        serializer = TraceSerializer()
        trace = serializer.deserialize(stored)
        assert trace.ai_configuration_version_id == "v-deser"
        assert trace.resolved_settings == {"router_threshold": 0.7}

    def test_deserialize_handles_missing_config_gracefully(self) -> None:
        """deserialize handles absence of config fields (backward compat)."""
        stored = {
            "trace_id": "t4",
            "route": "rag",
            "start_ts": "2024-01-01T00:00:00+00:00",
            "duration_ms": 50,
            "root_status": "success",
            "spans": [],
        }
        serializer = TraceSerializer()
        trace = serializer.deserialize(stored)
        assert trace.ai_configuration_version_id is None
        assert trace.resolved_settings == {}

    def test_full_round_trip(self) -> None:
        """Full serialize→deserialize round-trip preserves config data."""
        original = Trace(
            trace_id="t5",
            route="database",
            start_ts=datetime(2024, 6, 15, 10, 30, tzinfo=timezone.utc),
            duration_ms=200,
            root_status="success",
            spans=[],
            ai_configuration_version_id="v-roundtrip",
            resolved_settings={
                "prompt": "answer",
                "model": "gemini-3.1-pro",
                "retrieval_settings": {"top_k": 20},
            },
        )
        serializer = TraceSerializer()
        stored = serializer.serialize(original)
        restored = serializer.deserialize(stored)
        assert restored.ai_configuration_version_id == "v-roundtrip"
        assert restored.resolved_settings == original.resolved_settings
