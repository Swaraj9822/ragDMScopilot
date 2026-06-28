"""Property tests for the span hierarchy (parent/child nesting).

Feature: ai-observability-platform.

These tests exercise the span hierarchy recorded by
:class:`rag_system.observability_tracing.recorder.SpanRecorder` when an arbitrary
nesting tree of ``record_span`` blocks is opened inside an open ``start_trace``
block, using an ENABLED sampler so every trace is recorded.

For an arbitrary nesting tree the test asserts the invariants that follow from
the natural call nesting (R1.3, R1.6):

- every child span's ``parent_span_id`` equals the ``span_id`` of the span that
  was active when it opened (the enclosing span, or the Root_Span for a
  top-level stage);
- every ``span_id`` within the trace is unique;
- every span carries a recorded start timestamp;
- after all the ``with`` blocks close, the active span resolves to ``None``
  (parent restoration, R1.6).

The nesting is modelled as a Hypothesis recursive tree and walked with a
recursive helper that opens ``record_span`` and records, for each span, the
active span id read *before* the block opens (the expected parent) and the
active span id read *inside* the block (the span's own id). The completed spans
are captured from the injected :class:`BoundedSpanBuffer` via ``drain()`` after
the trace closes, and the parent/child relationships are verified against the
generated nesting.

A fresh, isolated :class:`~rag_system.observability.MetricsRegistry` and a fresh
:class:`~rag_system.observability_tracing.buffers.BoundedSpanBuffer` are injected
per example so the enqueued spans can be inspected without touching the
process-wide default registry or buffer.
"""

from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.context import get_active_span_id
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators - constrained to the recorder's input domain.
# ---------------------------------------------------------------------------

# A request route for the enclosing trace.
_routes = st.text(min_size=1, max_size=40)

# A nesting tree of instrumented stages. Each node carries an operation label and
# a (possibly empty) list of child nodes. The recursive strategy keeps the tree
# small enough to drive cheaply while still exploring varied nesting shapes
# (deep chains, wide fan-out, and mixtures). The top-level value is a *forest*
# (list of nodes) so a trace can open several sibling stages directly under the
# Root_Span.
_operations = st.text(min_size=1, max_size=20)


def _node(children: st.SearchStrategy) -> st.SearchStrategy:
    """A single stage node: an operation label plus a forest of child stages."""
    return st.builds(
        lambda operation, kids: {"operation": operation, "children": kids},
        _operations,
        children,
    )


_nesting_trees = st.recursive(
    # Base case: a forest of leaf stages (no children).
    st.lists(_node(st.just([])), max_size=3),
    # Recursive case: a forest whose nodes may themselves contain a forest.
    lambda forest: st.lists(_node(forest), max_size=3),
    max_leaves=25,
)


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


def _walk(forest: list, recorder: SpanRecorder, captured: list) -> None:
    """Open a ``record_span`` for each node, recording (span_id, expected_parent).

    For each node the active span id is read *before* the block opens - this is
    the span that should become the node's parent (the enclosing span, or the
    Root_Span for a top-level node). Once the block is open the active span id is
    the node's own span_id. Children are then walked recursively so their parent
    is this node.
    """
    for node in forest:
        expected_parent = get_active_span_id()
        with recorder.record_span(node["operation"]):
            own_span_id = get_active_span_id()
            captured.append((own_span_id, expected_parent))
            _walk(node["children"], recorder, captured)


# ---------------------------------------------------------------------------
# Property 2 - span hierarchy reflects call nesting.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 2: Span hierarchy reflects call nesting
# Validates: Requirements 1.3, 1.6
@settings(max_examples=100)
@given(route=_routes, forest=_nesting_trees)
def test_span_hierarchy_reflects_call_nesting(route: str, forest: list) -> None:
    """Spans nest by call structure; ids are unique; parents restore to None.

    For an arbitrary nesting tree of ``record_span`` blocks opened inside an
    ``start_trace`` block, every enqueued child span's ``parent_span_id`` equals
    the span active when it opened (an enclosing span, or the Root_Span for a
    top-level stage), all span_ids within the trace are unique, every span has a
    recorded start timestamp, and after the blocks close the active span resolves
    to ``None`` (R1.3, R1.6).
    """
    recorder, buffer = _make_recorder()
    captured: list[tuple[str | None, str | None]] = []

    # No span is active before the trace opens.
    assert get_active_span_id() is None

    with recorder.start_trace(trace_id=None, route=route):
        # Inside the trace the Root_Span is the active span.
        root_span_id = get_active_span_id()
        assert root_span_id is not None
        _walk(forest, recorder, captured)
        # The trace block restores the Root_Span as active after each child closes.
        assert get_active_span_id() == root_span_id

    # After all the with-blocks close, the active span resolves to None (R1.6).
    assert get_active_span_id() is None

    # Both the child spans and the Root_Span are enqueued once their blocks close.
    drained = buffer.drain()

    # Exactly one Root_Span (null parent) plus one span per walked node.
    root_spans = [span for span in drained if span.parent_span_id is None]
    child_spans = [span for span in drained if span.parent_span_id is not None]
    assert len(root_spans) == 1
    assert root_spans[0].span_id == root_span_id
    assert len(child_spans) == len(captured)

    # All span_ids within the trace are unique (R1.3).
    all_ids = [span.span_id for span in drained]
    assert len(all_ids) == len(set(all_ids))

    # Every span has a recorded start timestamp (R1.3).
    for span in drained:
        assert isinstance(span.start_ts, datetime)

    # Each child span's recorded parent equals the span active when it opened:
    # the enclosing span, or the Root_Span for a top-level stage (R1.3).
    recorded = {span.span_id: span for span in drained}
    captured_parents = dict(captured)
    for span_id, expected_parent in captured:
        assert span_id in recorded, "every captured span must be enqueued"
        assert recorded[span_id].parent_span_id == expected_parent
        # The parent must be a real span within this trace (Root_Span or another
        # captured stage), so the hierarchy is fully reconstructable.
        if expected_parent == root_span_id:
            assert recorded[span_id].parent_span_id == root_span_id
        else:
            assert expected_parent in captured_parents
