"""Property test for Root_Span trace_id adoption / generation.

Feature: ai-observability-platform.

This test exercises
:class:`rag_system.observability_tracing.recorder.SpanRecorder` driven by an
*enabled* sampler (``TraceSampler(enabled=True, sample_rate=1.0)``) so that every
trace is recorded. It validates the Root_Span identity rules:

- R1.1 -- when an active/explicit trace_id is present (e.g. supplied via the
  ``X-Trace-Id`` header), the Trace's Root_Span is identified by exactly that
  trace_id and has a null parent.
- R1.2 -- when no trace_id is present, the recorder generates a new trace_id that
  is a 32-char lowercase hexadecimal string and is unique among all currently
  active Traces.

The recorder never touches the store directly: a completed Root_Span is handed
to a bounded in-memory :class:`BoundedSpanBuffer`, from which off-path workers
later drain. The active trace_id visible inside the ``with`` block is observed
through :func:`get_active_trace_id`, matching how the real pipeline reads
request identity.
"""

import re
import threading

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.context import get_active_trace_id
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler
from rag_system.observability import MetricsRegistry

# ---------------------------------------------------------------------------
# Smart generators - constrained to the recorder's input domain.
# ---------------------------------------------------------------------------

# An explicit trace_id is an opaque correlation string carried on the request
# (e.g. the X-Trace-Id header). Its exact value must be adopted verbatim, so a
# hex alphabet across a range of lengths is representative without being costly.
_explicit_trace_ids = st.text(
    alphabet="0123456789abcdef", min_size=1, max_size=40
)

# A route is an HTTP path label; its value never affects the identity rules.
_routes = st.text(alphabet="abcdefghijklmnopqrstuvwxyz/_-", min_size=1, max_size=24)

# The shape of a generated trace_id: exactly 32 lowercase hexadecimal chars.
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _make_recorder() -> tuple[SpanRecorder, BoundedSpanBuffer]:
    """Build a recorder with an enabled sampler and a fresh buffer/registry."""
    registry = MetricsRegistry()
    span_buffer = BoundedSpanBuffer(metrics=registry)
    recorder = SpanRecorder(
        sampler=TraceSampler(enabled=True, sample_rate=1.0),
        span_buffer=span_buffer,
        metrics=registry,
    )
    return recorder, span_buffer


# ---------------------------------------------------------------------------
# Property 1 - Root span adopts or generates a valid unique trace id.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 1: Root span adopts or generates a valid unique trace id
# Validates: Requirements 1.1
@settings(max_examples=100)
@given(trace_id=_explicit_trace_ids, route=_routes)
def test_explicit_trace_id_is_adopted_with_null_parent(
    trace_id: str, route: str
) -> None:
    """An explicit trace_id becomes the Root_Span's trace identity (R1.1).

    When ``start_trace`` is given an explicit trace_id, the active trace_id
    observable inside the block equals that value exactly, and the Root_Span
    enqueued on close is a root (``parent_span_id is None``).
    """
    recorder, span_buffer = _make_recorder()

    with recorder.start_trace(trace_id=trace_id, route=route) as root:
        # The Root_Span carries the adopted trace_id (visible via the context).
        assert get_active_trace_id() == trace_id
        # A real (non-noop) Root_Span was created with no parent.
        assert root.span_id != ""
        assert root.parent_span_id is None

    # Exactly the Root_Span was enqueued, and it is a root span.
    drained = span_buffer.drain()
    assert len(drained) == 1
    assert drained[0].parent_span_id is None
    assert drained[0].span_id == root.span_id


# Feature: ai-observability-platform, Property 1: Root span adopts or generates a valid unique trace id
# Validates: Requirements 1.2
@settings(max_examples=100)
@given(route=_routes)
def test_generated_trace_id_is_32_char_lowercase_hex(route: str) -> None:
    """With no trace_id present, a 32-char lowercase hex trace_id is generated (R1.2).

    The generated identity matches ``^[0-9a-f]{32}$`` and the Root_Span is a
    root (``parent_span_id is None``).
    """
    recorder, span_buffer = _make_recorder()

    with recorder.start_trace(trace_id=None, route=route) as root:
        generated = get_active_trace_id()
        assert generated is not None
        assert _HEX32.match(generated), generated
        assert root.parent_span_id is None

    drained = span_buffer.drain()
    assert len(drained) == 1
    assert drained[0].parent_span_id is None


# Feature: ai-observability-platform, Property 1: Root span adopts or generates a valid unique trace id
# Validates: Requirements 1.2
@settings(max_examples=100, deadline=None)
@given(n=st.integers(min_value=2, max_value=6), route=_routes)
def test_overlapping_generated_trace_ids_are_unique(n: int, route: str) -> None:
    """Generated trace_ids are unique among currently active traces (R1.2).

    ``n`` traces are opened on separate threads and held open simultaneously via
    a barrier, so they are all *active* at once. Each thread opens its own
    ``start_trace`` (with no trace_id) in a fresh thread context, captures the
    active trace_id while all traces overlap, then closes. Every captured
    trace_id must be a valid 32-char lowercase hex string, and all ``n`` must be
    distinct.
    """
    recorder, _ = _make_recorder()

    captured: list[str | None] = [None] * n
    errors: list[BaseException] = []
    # All threads rendezvous here so every trace is active before any closes.
    barrier = threading.Barrier(n)

    def _worker(index: int) -> None:
        try:
            with recorder.start_trace(trace_id=None, route=route):
                tid = get_active_trace_id()
                captured[index] = tid
                # Hold the trace open until every sibling trace is also active.
                barrier.wait(timeout=30)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
            try:
                barrier.abort()
            except Exception:
                pass

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    # Every generated id is a valid 32-char lowercase hex string ...
    for tid in captured:
        assert tid is not None
        assert _HEX32.match(tid), tid
    # ... and all overlapping active trace ids are unique.
    assert len(set(captured)) == n
