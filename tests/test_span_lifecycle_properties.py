"""Property tests for the span lifecycle (duration and status).

Feature: ai-observability-platform.

These tests exercise the span lifecycle recorded by
:class:`rag_system.observability_tracing.recorder.SpanRecorder` when wrapping an
instrumented stage in ``record_span`` inside an open ``start_trace`` block, using
an ENABLED sampler so every trace is recorded. They assert the invariants that
hold regardless of whether the stage body completes normally or raises:

- the recorded ``Span.duration_ms`` is a non-negative integer number of
  milliseconds (rounded to the nearest whole millisecond) (R1.4, R1.5, R4.3,
  R12.7);
- the ``Span.status`` is ``"success"`` when the body completes normally and
  ``"error"`` when it raises (R1.4, R1.5, R4.1);
- when the body raises, the exception propagates out of the ``record_span``
  with-block unchanged (asserted via ``pytest.raises``).

A fresh, isolated :class:`~rag_system.observability.MetricsRegistry` and a fresh
:class:`~rag_system.observability_tracing.buffers.BoundedSpanBuffer` are injected
per example so the enqueued span can be inspected via ``drain()`` without
touching the process-wide default registry or buffer.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators - constrained to the recorder's input domain.
# ---------------------------------------------------------------------------

# An operation label is the stage name passed to ``record_span`` (mirroring the
# ``timed`` helper's labels). Any non-empty string is valid; a modest size keeps
# generation cheap.
_operations = st.text(min_size=1, max_size=50)

# A request route for the enclosing trace.
_routes = st.text(min_size=1, max_size=40)

# Exception types that an instrumented stage might raise. A representative spread
# of builtin exception classes is enough; the property does not depend on the
# specific type beyond it being a real exception.
_EXC_TYPES = [
    ValueError,
    KeyError,
    RuntimeError,
    TypeError,
    OSError,
    ZeroDivisionError,
    Exception,
]


@st.composite
def _exceptions(draw: st.DrawFn) -> Exception:
    """Build an arbitrary exception instance (arbitrary type and message)."""
    exc_type = draw(st.sampled_from(_EXC_TYPES))
    message = draw(st.text(max_size=200))
    return exc_type(message)


def _make_recorder() -> tuple[SpanRecorder, BoundedSpanBuffer]:
    """Build a recorder with an ENABLED sampler and a fresh injected buffer.

    A force-on sampler (enabled, rate 1.0) guarantees every trace is recorded, so
    spans are always created and enqueued. The buffer and metrics registry are
    fresh and isolated per call.
    """
    registry = MetricsRegistry()
    buffer = BoundedSpanBuffer(metrics=registry)
    sampler = TraceSampler(enabled=True, sample_rate=1.0)
    recorder = SpanRecorder(sampler=sampler, span_buffer=buffer, metrics=registry)
    return recorder, buffer


# ---------------------------------------------------------------------------
# Property 3 - span lifecycle records non-negative integer duration and status.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 3: Span lifecycle records non-negative integer duration and correct status
# Validates: Requirements 1.4, 1.5, 4.1, 4.3, 12.7
@settings(max_examples=100)
@given(operation=_operations, route=_routes, exc=st.one_of(st.none(), _exceptions()))
def test_span_lifecycle_duration_and_status(
    operation: str, route: str, exc: Exception | None,
) -> None:
    """The recorded child span has int duration >= 0 and the correct status.

    For an arbitrary operation label and an arbitrary choice of raising or not
    (and an arbitrary exception when raising), running ``record_span`` inside an
    open ``start_trace`` block enqueues a child span whose ``duration_ms`` is a
    non-negative integer and whose ``status`` is ``"success"`` on normal
    completion and ``"error"`` when the body raises. When raising, the exception
    must propagate out of the ``record_span`` with-block unchanged.
    """
    recorder, buffer = _make_recorder()
    raises = exc is not None

    with recorder.start_trace(trace_id=None, route=route):
        if raises:
            # The exception must propagate out of the record_span with-block
            # (the recorder re-raises it after recording the span). Catching it
            # here keeps it from also failing the enclosing trace block.
            with pytest.raises(type(exc)):
                with recorder.record_span(operation):
                    raise exc
        else:
            with recorder.record_span(operation):
                pass

    # Both the child span and the Root_Span are enqueued once their blocks close.
    drained = buffer.drain()

    # The child span is the one with a non-null parent (the Root_Span has none).
    child_spans = [span for span in drained if span.parent_span_id is not None]
    assert len(child_spans) == 1, "expected exactly one enqueued child span"
    child = child_spans[0]

    # Duration is a non-negative integer number of milliseconds (R1.4/R1.5/R4.3).
    assert isinstance(child.duration_ms, int)
    assert not isinstance(child.duration_ms, bool)
    assert child.duration_ms >= 0

    # Status reflects the outcome of the body (R1.4 success / R1.5/R4.1 error).
    expected_status = "error" if raises else "success"
    assert child.status == expected_status

    # The recorded span carries the operation label that was opened.
    assert child.operation == operation
