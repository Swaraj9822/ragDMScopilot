"""Property tests for operation metrics parity between record_span and timed helper.

Feature: ai-observability-platform.

These tests verify that :meth:`SpanRecorder.record_span` emits exactly the same
operation metrics as the old :func:`~rag_system.observability.timed` helper —
namely ``rag_operation_total`` incremented once with the correct operation/status
labels, and ``rag_operation_duration_ms`` observed once with the operation label
and the measured duration — for both success and error outcomes (R11.5, R11.6).

The property is exercised across arbitrary operation names and both outcome
paths, using a spy metrics registry to capture all emitted metrics and verify
parity with the expected ``timed`` helper behaviour.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Spy metrics registry — records all increment/observe calls for assertions.
# ---------------------------------------------------------------------------


class MetricsSpy(MetricsRegistry):
    """A metrics registry that also records raw calls for inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.increments: list[tuple[str, dict]] = []
        self.observations: list[tuple[str, float, dict]] = []

    def increment(self, name: str, labels: dict | None = None, amount: float = 1.0) -> None:
        self.increments.append((name, dict(labels) if labels else {}))
        super().increment(name, labels, amount)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        self.observations.append((name, value, dict(labels) if labels else {}))
        super().observe(name, value, labels)


# ---------------------------------------------------------------------------
# Smart generators
# ---------------------------------------------------------------------------

# Operation names: non-empty printable strings matching realistic operation labels.
# We exclude "Root_Span" because that's the reserved operation name used by the
# root span itself — using it as a child operation would conflate metrics.
_operation_names = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S"), min_codepoint=33),
    min_size=1,
    max_size=40,
).filter(lambda s: s != "Root_Span")


def _make_recorder_with_spy() -> tuple[SpanRecorder, BoundedSpanBuffer, MetricsSpy]:
    """Build a recorder whose metrics object is a spy, with tracing enabled."""
    spy = MetricsSpy()
    buffer = BoundedSpanBuffer(metrics=spy)
    sampler = TraceSampler(enabled=True, sample_rate=1.0)
    recorder = SpanRecorder(sampler=sampler, span_buffer=buffer, metrics=spy)
    return recorder, buffer, spy


# ---------------------------------------------------------------------------
# Property 24 — Operation metrics mirror the timed helper
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 24: Operation metrics mirror the timed helper
# Validates: Requirements 11.5, 11.6
@settings(max_examples=100)
@given(operation=_operation_names)
def test_record_span_success_emits_operation_total_and_duration(
    operation: str,
) -> None:
    """On successful completion, record_span emits rag_operation_total with
    status=success and rag_operation_duration_ms with the operation label,
    matching the timed helper's success path (R11.5).
    """
    recorder, _, spy = _make_recorder_with_spy()

    with recorder.start_trace(trace_id=None, route="test"):
        with recorder.record_span(operation):
            pass

    # Filter to metrics emitted by the child span (not the root span).
    child_increments = [
        (name, labels)
        for name, labels in spy.increments
        if labels.get("operation") == operation
    ]
    child_observations = [
        (name, value, labels)
        for name, value, labels in spy.observations
        if labels.get("operation") == operation
    ]

    # Exactly one rag_operation_total increment with success status.
    total_calls = [
        (name, labels)
        for name, labels in child_increments
        if name == "rag_operation_total"
    ]
    assert len(total_calls) == 1, f"Expected 1 total increment, got {len(total_calls)}"
    assert total_calls[0][1] == {"operation": operation, "status": "success"}

    # Exactly one rag_operation_duration_ms observation.
    duration_calls = [
        (name, value, labels)
        for name, value, labels in child_observations
        if name == "rag_operation_duration_ms"
    ]
    assert len(duration_calls) == 1, f"Expected 1 duration observation, got {len(duration_calls)}"
    assert duration_calls[0][2] == {"operation": operation, "status": "success"}
    # Duration is a non-negative number of milliseconds.
    assert duration_calls[0][1] >= 0.0


# Feature: ai-observability-platform, Property 24: Operation metrics mirror the timed helper
# Validates: Requirements 11.5, 11.6
@settings(max_examples=100)
@given(operation=_operation_names)
def test_record_span_error_emits_operation_total_and_duration_with_error_status(
    operation: str,
) -> None:
    """On failure (exception), record_span emits rag_operation_total with
    status=error and rag_operation_duration_ms with the operation label,
    matching the timed helper's error path (R11.6).
    """
    recorder, _, spy = _make_recorder_with_spy()

    class _TestError(Exception):
        pass

    with recorder.start_trace(trace_id=None, route="test"):
        try:
            with recorder.record_span(operation):
                raise _TestError("simulated failure")
        except _TestError:
            pass

    # Filter to metrics emitted by the child span (not the root span).
    child_increments = [
        (name, labels)
        for name, labels in spy.increments
        if labels.get("operation") == operation
    ]
    child_observations = [
        (name, value, labels)
        for name, value, labels in spy.observations
        if labels.get("operation") == operation
    ]

    # Exactly one rag_operation_total increment with error status.
    total_calls = [
        (name, labels)
        for name, labels in child_increments
        if name == "rag_operation_total"
    ]
    assert len(total_calls) == 1, f"Expected 1 total increment, got {len(total_calls)}"
    assert total_calls[0][1] == {"operation": operation, "status": "error"}

    # Exactly one rag_operation_duration_ms observation with error status.
    duration_calls = [
        (name, value, labels)
        for name, value, labels in child_observations
        if name == "rag_operation_duration_ms"
    ]
    assert len(duration_calls) == 1, f"Expected 1 duration observation, got {len(duration_calls)}"
    assert duration_calls[0][2] == {"operation": operation, "status": "error"}
    # Duration is a non-negative number of milliseconds.
    assert duration_calls[0][1] >= 0.0
