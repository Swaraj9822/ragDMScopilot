"""Property test for trace serialization round-trip identity.

Feature: ai-observability-platform.

This module exercises :class:`rag_system.observability_tracing.serializer.TraceSerializer`
across arbitrary valid :class:`Trace` objects, asserting that

    deserialize(serialize(trace))

reproduces an *equivalent* Trace per the design's Property 6 / R6.3 definition of
equivalence: identical ``trace_id``, identical ``route``, and a span set matched
by ``span_id`` that agrees on the parent relationship (``parent_span_id``),
``duration_ms``, ``status``, and the complete set of attribute key-value pairs —
with no span, attribute, or value omitted, added, or altered.

Generators are constrained to the serializer's valid input domain (R5.2, R5.3,
R6.1, R6.2):

- 1..N spans where the first is the Root_Span (``parent_span_id`` is ``None``)
  and every subsequent span references an already-generated span_id as its
  parent, so every parent reference resolves within the trace;
- span_ids unique within a trace;
- scalar attribute values spanning ``str``/``int``/``float``/``bool`` (NaN
  floats excluded so equality is exact and the round-trip instant is preserved);
- timezone-aware UTC start timestamps and non-negative integer durations;
- statuses drawn from ``{"success", "error"}``.

Timestamps are compared as UTC *instants*: ``serialize`` renders ISO-8601 UTC and
``deserialize`` returns aware UTC datetimes, so the round-trip preserves the
instant even though tzinfo representation is normalised.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import Span, Trace
from rag_system.observability_tracing.serializer import TraceSerializer

# ---------------------------------------------------------------------------
# Smart generators - constrained to the serializer's valid input domain.
# ---------------------------------------------------------------------------

_STATUSES = st.sampled_from(["success", "error"])

# Span and trace identifiers: non-empty identifier-ish strings. span_ids are kept
# unique within a trace via a dedicated lists(..., unique=True) draw below.
_span_ids = st.text(
    alphabet="abcdef0123456789", min_size=4, max_size=16
)

# A 32-char lowercase hex trace_id (matches the domain note); any non-empty
# string is accepted by the serializer, but using a realistic shape keeps the
# generated traces representative.
_trace_ids = st.text(alphabet="abcdef0123456789", min_size=32, max_size=32)

_routes = st.text(min_size=1, max_size=40)

# Non-negative integer durations within a generous bound.
_durations = st.integers(min_value=0, max_value=10_000_000)

# Timezone-aware UTC datetimes. ``allow_imaginary`` is irrelevant for UTC; we pin
# tzinfo to UTC so generated instants are unambiguous and the round-trip is exact.
_utc_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(timezone.utc),
)

# Scalar attribute values: str | int | float | bool, excluding NaN so equality is
# exact. bool is generated explicitly (it is a subclass of int, but a distinct
# attribute value here). Floats exclude nan/inf to keep round-trip equality exact.
_attr_values = st.one_of(
    st.text(max_size=40),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.booleans(),
)

_attr_keys = st.text(min_size=1, max_size=20)

_attributes = st.dictionaries(keys=_attr_keys, values=_attr_values, max_size=6)


@st.composite
def _traces(draw: st.DrawFn) -> Trace:
    """Build a valid :class:`Trace` with 1..N spans and resolvable parents.

    The first span is the Root_Span (``parent_span_id is None``). Every later
    span picks its parent from the set of span_ids already generated, so all
    parent references resolve within the trace and the hierarchy is well-formed.
    """
    trace_id = draw(_trace_ids)
    route = draw(_routes)

    # Unique span_ids; at least one (the Root_Span).
    span_ids = draw(
        st.lists(_span_ids, min_size=1, max_size=8, unique=True)
    )

    spans: list[Span] = []
    for index, span_id in enumerate(span_ids):
        if index == 0:
            parent_span_id: str | None = None
        else:
            # Reference any already-generated span as the parent.
            parent_span_id = draw(st.sampled_from(span_ids[:index]))

        spans.append(
            Span(
                span_id=span_id,
                parent_span_id=parent_span_id,
                operation=draw(st.text(min_size=1, max_size=20)),
                start_ts=draw(_utc_datetimes),
                duration_ms=draw(_durations),
                status=draw(_STATUSES),
                attributes=draw(_attributes),
            )
        )

    return Trace(
        trace_id=trace_id,
        route=route,
        start_ts=draw(_utc_datetimes),
        duration_ms=draw(_durations),
        root_status=draw(_STATUSES),
        spans=spans,
    )


def _same_instant(a: datetime, b: datetime) -> bool:
    """True when two datetimes denote the same UTC instant."""
    return a.astimezone(timezone.utc) == b.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Property 6 - serialization round-trip is identity.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 6: Trace serialization round-trip is identity
# Validates: Requirements 5.2, 5.3, 6.1, 6.2, 6.3
@settings(max_examples=100)
@given(trace=_traces())
def test_trace_serialization_round_trip_is_identity(trace: Trace) -> None:
    """``deserialize(serialize(trace))`` is equivalent to the original (R6.3).

    Equivalence: identical trace_id and route, and a span set matched by span_id
    that agrees on parent_span_id, duration_ms, status, and the complete attribute
    key-value map. Start timestamps are compared as UTC instants.
    """
    serializer = TraceSerializer()

    restored = serializer.deserialize(serializer.serialize(trace))

    # Trace-level identity.
    assert restored.trace_id == trace.trace_id
    assert restored.route == trace.route
    assert restored.duration_ms == trace.duration_ms
    assert restored.root_status == trace.root_status
    assert _same_instant(restored.start_ts, trace.start_ts)

    # Span set matched by span_id - no span omitted, added, or altered.
    original_by_id = {span.span_id: span for span in trace.spans}
    restored_by_id = {span.span_id: span for span in restored.spans}
    assert set(restored_by_id) == set(original_by_id)
    assert len(restored.spans) == len(trace.spans)

    for span_id, original_span in original_by_id.items():
        restored_span = restored_by_id[span_id]
        assert restored_span.parent_span_id == original_span.parent_span_id
        assert restored_span.operation == original_span.operation
        assert restored_span.duration_ms == original_span.duration_ms
        assert restored_span.status == original_span.status
        assert _same_instant(restored_span.start_ts, original_span.start_ts)

        # The complete set of attribute key-value pairs is preserved verbatim.
        assert set(restored_span.attributes) == set(original_span.attributes)
        for key, value in original_span.attributes.items():
            restored_value = restored_span.attributes[key]
            assert type(restored_value) is type(value)
            if isinstance(value, float):
                assert not math.isnan(restored_value)
            assert restored_value == value
