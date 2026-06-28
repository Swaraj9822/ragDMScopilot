"""Property test for malformed stored-trace deserialization.

Feature: ai-observability-platform.

Property 7: Malformed stored traces fail cleanly.

*For any* stored trace representation that is missing a required field (trace_id,
span identifier, span parent reference, duration, or status) or cannot otherwise
be parsed, :meth:`TraceSerializer.deserialize` raises a
:class:`TraceDeserializationError` indicating the malformation *reason* together
with the affected *trace_id* (where determinable), and never returns a partially
deserialized :class:`Trace`.

Strategy: start from a *valid* generated :class:`Trace`, serialize it to a
well-formed :class:`StoredTrace` (which is known to deserialize cleanly), then
mutate exactly one required field into a malformed state and assert that
deserialization fails cleanly. Mutations cover both trace-level fields
(``trace_id``, ``route``, ``start_ts``, ``duration_ms``, ``root_status``) and
span-level fields (``span_id``, the ``parent_span_id`` key, ``start_ts``,
``duration_ms``, ``status``).

When the trace_id itself is the corrupted field it is no longer determinable as
the original id, so the affected-trace_id equality assertion is only made for the
mutations that leave a valid ``trace_id`` in place.

**Validates: Requirements 6.4**
"""

from __future__ import annotations

import copy
import string
from datetime import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import Span, Trace
from rag_system.observability_tracing.serializer import (
    TraceDeserializationError,
    TraceSerializer,
)

# ---------------------------------------------------------------------------
# Smart generators - constrained to the serializer's valid input domain so the
# baseline StoredTrace always serializes/deserializes cleanly before mutation.
# ---------------------------------------------------------------------------

# 32-char lowercase hex trace ids (model contract); the serializer accepts any
# non-empty string, but realistic ids keep the baseline faithful.
_trace_ids = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)

_span_ids = st.text(
    alphabet=string.ascii_letters + string.digits, min_size=1, max_size=16
)
_routes = st.text(min_size=1, max_size=40)
_operations = st.text(min_size=1, max_size=20)
_statuses = st.sampled_from(["success", "error"])
_durations = st.integers(min_value=0, max_value=86_400_000)
_timestamps = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)
)

# Scalar attribute values only (coerced at capture time, preserved verbatim).
_attr_values = st.one_of(
    st.text(max_size=20),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
)
_attributes = st.dictionaries(
    st.text(min_size=1, max_size=12), _attr_values, max_size=4
)


@st.composite
def _spans(draw: st.DrawFn) -> Span:
    return Span(
        span_id=draw(_span_ids),
        parent_span_id=draw(st.one_of(st.none(), _span_ids)),
        operation=draw(_operations),
        start_ts=draw(_timestamps),
        duration_ms=draw(_durations),
        status=draw(_statuses),
        attributes=draw(_attributes),
    )


@st.composite
def _traces(draw: st.DrawFn) -> Trace:
    """A valid :class:`Trace` with one or more spans (serializes cleanly)."""
    return Trace(
        trace_id=draw(_trace_ids),
        route=draw(_routes),
        start_ts=draw(_timestamps),
        duration_ms=draw(_durations),
        root_status=draw(_statuses),
        spans=draw(st.lists(_spans(), min_size=1, max_size=5)),
    )


# ---------------------------------------------------------------------------
# Malformations. Each entry maps a kind to whether the trace_id stays
# determinable as the original id after the mutation is applied.
# ---------------------------------------------------------------------------

# Trace-level mutations. Only the trace_id corruptions destroy determinability.
_TRACE_MUTATIONS = {
    "del_trace_id": False,
    "empty_trace_id": False,
    "nonstr_trace_id": False,
    "del_route": True,
    "del_start_ts": True,
    "corrupt_start_ts": True,
    "del_duration": True,
    "negative_duration": True,
    "del_root_status": True,
    "corrupt_root_status": True,
}

# Span-level mutations always leave the trace_id intact (captured first).
_SPAN_MUTATIONS = (
    "del_span_id",
    "empty_span_id",
    "del_parent_key",
    "corrupt_parent",
    "del_span_start_ts",
    "corrupt_span_start_ts",
    "del_span_duration",
    "negative_span_duration",
    "del_span_status",
    "corrupt_span_status",
)

_ALL_MUTATIONS = tuple(_TRACE_MUTATIONS) + _SPAN_MUTATIONS


def _apply_mutation(stored: dict, kind: str, span_index: int) -> None:
    """Mutate *stored* in place into a single-field malformation of *kind*."""
    # --- trace-level ----------------------------------------------------
    if kind == "del_trace_id":
        del stored["trace_id"]
    elif kind == "empty_trace_id":
        stored["trace_id"] = ""
    elif kind == "nonstr_trace_id":
        stored["trace_id"] = 12345
    elif kind == "del_route":
        del stored["route"]
    elif kind == "del_start_ts":
        del stored["start_ts"]
    elif kind == "corrupt_start_ts":
        stored["start_ts"] = "not-a-timestamp"
    elif kind == "del_duration":
        del stored["duration_ms"]
    elif kind == "negative_duration":
        stored["duration_ms"] = -1
    elif kind == "del_root_status":
        del stored["root_status"]
    elif kind == "corrupt_root_status":
        stored["root_status"] = "bogus-status"
    # --- span-level -----------------------------------------------------
    elif kind == "del_span_id":
        del stored["spans"][span_index]["span_id"]
    elif kind == "empty_span_id":
        stored["spans"][span_index]["span_id"] = ""
    elif kind == "del_parent_key":
        del stored["spans"][span_index]["parent_span_id"]
    elif kind == "corrupt_parent":
        stored["spans"][span_index]["parent_span_id"] = 999
    elif kind == "del_span_start_ts":
        del stored["spans"][span_index]["start_ts"]
    elif kind == "corrupt_span_start_ts":
        stored["spans"][span_index]["start_ts"] = "nope"
    elif kind == "del_span_duration":
        del stored["spans"][span_index]["duration_ms"]
    elif kind == "negative_span_duration":
        stored["spans"][span_index]["duration_ms"] = -7
    elif kind == "del_span_status":
        del stored["spans"][span_index]["status"]
    elif kind == "corrupt_span_status":
        stored["spans"][span_index]["status"] = "weird"
    else:  # pragma: no cover - guards against an unhandled mutation kind
        raise AssertionError(f"unknown mutation kind: {kind}")


# ---------------------------------------------------------------------------
# Property 7 - malformed stored traces fail cleanly.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 7: Malformed stored traces fail cleanly
# Validates: Requirements 6.4
@settings(max_examples=100)
@given(trace=_traces(), kind=st.sampled_from(_ALL_MUTATIONS), data=st.data())
def test_malformed_stored_trace_fails_cleanly(
    trace: Trace, kind: str, data: st.DataObject
) -> None:
    """Deserializing a single-field malformation raises, never a partial Trace.

    A valid trace is serialized (the baseline deserializes cleanly), then one
    required field is corrupted or removed. ``deserialize`` must raise a
    :class:`TraceDeserializationError` whose ``reason`` is a non-empty string and,
    where the trace_id is still determinable, whose ``trace_id`` equals the
    original trace's id. No (partial) Trace is ever returned (R6.4).
    """
    serializer = TraceSerializer()

    # Baseline: the unmutated stored representation deserializes cleanly.
    stored = serializer.serialize(trace)
    rebuilt = serializer.deserialize(stored)
    assert isinstance(rebuilt, Trace)

    # Mutate exactly one required field into a malformed state.
    mutated = copy.deepcopy(dict(stored))
    span_index = data.draw(st.integers(min_value=0, max_value=len(trace.spans) - 1))
    _apply_mutation(mutated, kind, span_index)

    # Deserialization must fail cleanly: an exception is raised and no value
    # (partial Trace) is returned.
    with pytest.raises(TraceDeserializationError) as exc_info:
        serializer.deserialize(mutated)

    err = exc_info.value

    # The malformation reason is a non-empty string indicating what went wrong.
    assert isinstance(err.reason, str)
    assert err.reason != ""

    determinable = _TRACE_MUTATIONS.get(kind, True)
    if determinable:
        # The affected trace_id is reported as the original trace's id.
        assert err.trace_id == trace.trace_id
    else:
        # The trace_id itself was corrupted, so it is not the original id.
        assert err.trace_id != trace.trace_id
