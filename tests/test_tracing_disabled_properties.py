"""Property test for tracing-disabled behaviour.

Feature: ai-observability-platform.

This test exercises :class:`rag_system.observability_tracing.recorder.SpanRecorder`
when it is constructed with a *disabled* sampler
(``TraceSampler(enabled=False, ...)``). Per R10.1/R10.8, while tracing is
disabled the platform must skip Span creation and perform zero Trace_Store
writes, ignoring any ``X-Trace-Id`` header.

The recorder never touches the store directly: completed spans are handed to a
bounded in-memory buffer (:class:`BoundedSpanBuffer`) from which off-path flush
workers later drain and persist. "Zero store writes" therefore reduces, at the
recorder level, to "nothing is ever enqueued to the span buffer". The test
mirrors the real request flow: the middleware always opens ``start_trace`` and
the pipeline stages always call ``record_span`` inside it -- so the property
must hold across an arbitrary sequence of ``record_span`` operations, with and
without a supplied trace header.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators - constrained to the recorder's input domain.
# ---------------------------------------------------------------------------

# A route is an HTTP path label; its value never affects the disabled decision.
_routes = st.text(alphabet="abcdefghijklmnopqrstuvwxyz/_-", min_size=1, max_size=24)

# A trace_id is an opaque correlation string, or absent. Both the "header
# present" and "header absent" cases must be covered (R10.8): when disabled the
# header is ignored and the request is still treated as not sampled.
_trace_ids = st.one_of(
    st.none(),
    st.text(alphabet="0123456789abcdef", min_size=1, max_size=32),
)

# An operation label for an instrumented pipeline stage span.
_operations = st.text(alphabet="abcdefghijklmnopqrstuvwxyz _", min_size=1, max_size=24)

# A finite, in-range sampling rate; irrelevant while disabled but varied to show
# the disabled decision does not depend on it (R10.1).
_rates = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property 23 - tracing-disabled performs no span creation or store writes.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 23: Tracing-disabled performs no span creation or store writes
# Validates: Requirements 10.1
@settings(max_examples=100)
@given(
    rate=_rates,
    trace_id=_trace_ids,
    route=_routes,
    operations=st.lists(_operations, min_size=0, max_size=6),
)
def test_disabled_recorder_never_writes_spans(
    rate: float,
    trace_id: str | None,
    route: str,
    operations: list[str],
) -> None:
    """A disabled recorder enqueues no spans for any trace/route/operation sequence.

    For any configured rate, any trace_id (present, i.e. an ``X-Trace-Id``
    header, or absent), any route, and any sequence of ``record_span`` calls
    nested inside ``start_trace``, the span buffer must remain empty and no
    operation may raise (R10.1/R10.8).
    """
    registry = MetricsRegistry()
    span_buffer = BoundedSpanBuffer(metrics=registry)
    recorder = SpanRecorder(
        sampler=TraceSampler(enabled=False, sample_rate=rate),
        span_buffer=span_buffer,
        metrics=registry,
    )

    with recorder.start_trace(trace_id=trace_id, route=route) as root:
        # When disabled, start_trace yields a no-op span sentinel: no real span
        # was created (empty span_id) so nothing can be persisted for it.
        assert root.span_id == ""
        for operation in operations:
            with recorder.record_span(operation):
                pass

    # No span -- neither the Root_Span nor any stage span -- was ever enqueued
    # for persistence: zero store writes (R10.1).
    assert len(span_buffer) == 0
    assert span_buffer.drain() == []
