"""Property test for trace retrieval returning the full, hierarchy-reconstructable
span set.

Feature: ai-observability-platform.

This module exercises trace retrieval through the in-memory transactional store
double (``tests/observability_tracing_store_double.py``), the same double the
spec designates for the persistence/retrieval property tests (task 8.2). For any
valid :class:`Trace` persisted into the double, ``get_trace(trace_id)`` must,
per the design's Property 9 / R7.1, R7.2, R7.5:

- return **exactly** the persisted set of spans (none omitted, none added);
- preserve each span's ``parent_span_id`` verbatim, with the Root_Span's parent
  being an explicit ``None``;
- order the spans ascending by span start timestamp, breaking ties between spans
  that share an identical start timestamp by ascending ``span_id``.

Generators are constrained to the valid trace domain (a well-formed hierarchy:
the first span is the Root_Span with a ``None`` parent and every later span
references an already-generated span as its parent). Start timestamps are drawn
from a deliberately small pool so that ties are common, making the span_id
tie-break a frequently-exercised part of the property rather than a rare edge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import Span, Trace

# Test-support module (not collected by pytest); imported by module name, the
# repo's convention for shared test fakes (see test_preservation_properties.py).
from observability_tracing_store_double import InMemoryTransactionalStore

# ---------------------------------------------------------------------------
# Smart generators - constrained to a valid, well-formed trace.
# ---------------------------------------------------------------------------

_STATUSES = st.sampled_from(["success", "error"])

# 32-char lowercase hex trace_id (the domain's required identifier shape).
_trace_ids = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)

# Span identifiers: short hex-ish strings, kept unique within a trace via the
# lists(..., unique=True) draw below.
_span_ids = st.text(alphabet="0123456789abcdef", min_size=4, max_size=12)

_routes = st.text(min_size=1, max_size=40)
_durations = st.integers(min_value=0, max_value=10_000_000)

# A small pool of distinct UTC start timestamps. Drawing span start timestamps
# from a small set guarantees frequent ties, so the ascending-span_id tie-break
# (R7.2) is exercised on most examples instead of only rarely.
_BASE_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_ts_pool = st.sampled_from(
    [_BASE_TS + timedelta(seconds=offset) for offset in (0, 1, 2, 3)]
)

_attr_values = st.one_of(
    st.text(max_size=20),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.booleans(),
)
_attributes = st.dictionaries(
    keys=st.text(min_size=1, max_size=12), values=_attr_values, max_size=4
)


@st.composite
def _traces(draw: st.DrawFn) -> Trace:
    """Build a valid :class:`Trace` with 1..N spans and a resolvable hierarchy.

    The first span is the Root_Span (``parent_span_id is None``); every later
    span draws its parent from the span_ids already generated, so the hierarchy
    is well-formed and reconstructable. Start timestamps are drawn from a small
    pool to make ties (and thus the span_id tie-break) common.
    """
    span_ids = draw(st.lists(_span_ids, min_size=1, max_size=8, unique=True))

    spans: list[Span] = []
    for index, span_id in enumerate(span_ids):
        parent_span_id = None if index == 0 else draw(st.sampled_from(span_ids[:index]))
        spans.append(
            Span(
                span_id=span_id,
                parent_span_id=parent_span_id,
                operation=draw(st.text(min_size=1, max_size=20)),
                start_ts=draw(_ts_pool),
                duration_ms=draw(_durations),
                status=draw(_STATUSES),
                attributes=draw(_attributes),
            )
        )

    return Trace(
        trace_id=draw(_trace_ids),
        route=draw(_routes),
        start_ts=draw(_ts_pool),
        duration_ms=draw(_durations),
        root_status=draw(_STATUSES),
        spans=spans,
    )


# ---------------------------------------------------------------------------
# Property 9 - trace retrieval returns the full, hierarchy-reconstructable span set.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 9: Trace retrieval returns the full, hierarchy-reconstructable span set
# Validates: Requirements 7.1, 7.2, 7.5
@settings(max_examples=100)
@given(trace=_traces())
def test_trace_retrieval_returns_full_hierarchy_reconstructable_span_set(
    trace: Trace,
) -> None:
    """``get_trace`` returns exactly the persisted spans, parents preserved
    (null for the root), ordered by start ts then ascending span_id (R7.1/7.2/7.5).
    """
    store = InMemoryTransactionalStore()
    store.persist(trace)

    fetched = store.get_trace(trace.trace_id)

    # R7.1 - a persisted trace is returned by its id.
    assert fetched is not None
    assert fetched.trace_id == trace.trace_id

    original_by_id = {span.span_id: span for span in trace.spans}
    fetched_by_id = {span.span_id: span for span in fetched.spans}

    # R7.1 - exactly the persisted span set: none omitted, none added.
    assert set(fetched_by_id) == set(original_by_id)
    assert len(fetched.spans) == len(trace.spans)

    # R7.5 - each span's parent_span_id is preserved; the root's parent is null.
    for span_id, original_span in original_by_id.items():
        assert fetched_by_id[span_id].parent_span_id == original_span.parent_span_id
    roots = [span for span in fetched.spans if span.parent_span_id is None]
    assert len(roots) == 1
    assert roots[0].span_id == trace.spans[0].span_id

    # R7.2 - ascending by start timestamp, ties broken by ascending span_id.
    order_keys = [(span.start_ts, span.span_id) for span in fetched.spans]
    assert order_keys == sorted(order_keys)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
