"""Property test for exception capture on the span recorder.

Feature: ai-observability-platform.

This test exercises :class:`rag_system.observability_tracing.recorder.SpanRecorder`
on the error path. Per R4.2/R4.4, when a pipeline exception propagates out of an
instrumented span the recorder must record the exception type (attribute
``exception.type``) and a message attribute (``exception.message``) truncated to
at most :data:`MAX_EXCEPTION_MESSAGE_LENGTH` (4096) characters, and re-raise the
*original* exception unchanged (same type and message).

The recorder never touches the store directly: completed spans -- including the
errored one -- are handed to a bounded in-memory buffer
(:class:`BoundedSpanBuffer`) from which off-path flush workers later drain. The
test injects a fresh :class:`MetricsRegistry` and :class:`BoundedSpanBuffer` per
example, runs ``record_span`` inside an ENABLED ``start_trace``, and inspects the
drained span.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import (
    MAX_EXCEPTION_MESSAGE_LENGTH,
    SpanRecorder,
)
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators - constrained to the recorder's error-path input domain.
# ---------------------------------------------------------------------------

# A small set of builtin exception classes a pipeline stage might raise. All are
# constructed from a single string argument; ``str(exc)`` round-trips through
# the same coercion the recorder applies, so the property holds uniformly across
# the set (including KeyError, whose ``str`` adds quotes).
_EXCEPTION_CLASSES = [
    ValueError,
    RuntimeError,
    TypeError,
    KeyError,
    IndexError,
    OSError,
]
_exception_classes = st.sampled_from(_EXCEPTION_CLASSES)

# Messages span the truncation boundary: many are short, but max_size well above
# 4096 ensures Hypothesis also explores messages longer than the cap (R4.2).
_messages = st.text(min_size=0, max_size=8192)

# A route is an HTTP path label; an operation labels an instrumented stage span.
_routes = st.text(alphabet="abcdefghijklmnopqrstuvwxyz/_-", min_size=1, max_size=24)
_operations = st.text(alphabet="abcdefghijklmnopqrstuvwxyz _", min_size=1, max_size=24)


# ---------------------------------------------------------------------------
# Property 4 - exceptions recorded and re-raised unchanged with bounded message.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 4: Exceptions are recorded and re-raised unchanged with bounded message
# Validates: Requirements 4.2, 4.4
@settings(max_examples=100)
@given(
    exc_class=_exception_classes,
    message=_messages,
    route=_routes,
    operation=_operations,
)
def test_exception_recorded_and_reraised_unchanged_with_bounded_message(
    exc_class: type[Exception],
    message: str,
    route: str,
    operation: str,
) -> None:
    """An exception raised in a span is recorded with a bounded message and re-raised unchanged.

    For an arbitrary builtin exception type and message (including messages
    longer than 4096 chars), the recorder must:
    - re-raise the original exception out of the ``with`` block unchanged (same
      instance, hence same type and ``str`` value) -- R4.4;
    - record ``exception.type`` == the exception class name and
      ``exception.message`` == ``str(exc)`` truncated to <= 4096 chars -- R4.2.
    """
    registry = MetricsRegistry()
    span_buffer = BoundedSpanBuffer(metrics=registry)
    recorder = SpanRecorder(
        sampler=TraceSampler(enabled=True, sample_rate=1.0),
        span_buffer=span_buffer,
        metrics=registry,
    )

    original = exc_class(message)
    # The recorder applies str(exc)[:MAX]; compute the expectation from the same
    # source so the boundary (e.g. KeyError's quoting) is handled correctly.
    expected_message = str(original)[:MAX_EXCEPTION_MESSAGE_LENGTH]

    with pytest.raises(exc_class) as excinfo:
        with recorder.start_trace(trace_id=None, route=route):
            with recorder.record_span(operation):
                raise original

    # R4.4: the original exception propagates out unchanged -- same instance,
    # therefore identical type and message.
    assert excinfo.value is original
    assert type(excinfo.value) is exc_class
    assert str(excinfo.value) == str(original)

    # R4.2: the errored child span was enqueued with the exception recorded.
    spans = span_buffer.drain()
    matching = [
        span
        for span in spans
        if span.operation == operation and "exception.type" in span.attributes
    ]
    assert matching, "expected the errored stage span to be enqueued with exception attrs"
    span = matching[0]

    assert span.status == "error"
    assert span.attributes["exception.type"] == exc_class.__name__
    assert span.attributes["exception.message"] == expected_message
    # The recorded message never exceeds the configured cap.
    assert len(span.attributes["exception.message"]) <= MAX_EXCEPTION_MESSAGE_LENGTH
