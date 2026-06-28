"""Property tests for span attribute scalar coercion.

Feature: ai-observability-platform.

These tests exercise the attribute-recording layer of
:class:`rag_system.observability_tracing.recorder.SpanRecorder`. They assert the
core scalar invariant (R3.7): every value attached to a :class:`Span` via
``set_attributes`` is stored as one of the permitted scalar types — ``str``,
``int``, ``float`` or ``bool`` — with native scalars preserved by type and value
and every non-scalar replaced by its ``str()`` representation. They also assert
that the stage-specific helpers record the required attributes with the
specified scalar types (R3.1, R3.3, R3.5, R3.6), including a document id always
recorded as a string regardless of the input's validity (R3.6).

The recorder is built with an ENABLED sampler so that ``record_span`` (which
routes its attributes through ``set_attributes``) also produces real spans, but
the scalar invariant is most directly exercised by calling ``set_attributes`` on
a freshly constructed :class:`Span`.
"""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.models import Span
from rag_system.observability_tracing.recorder import (
    NO_SCORE,
    UNAVAILABLE,
    SpanRecorder,
)
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators - the full Python-value input domain of set_attributes.
# ---------------------------------------------------------------------------

# Native scalar values: the four permitted attribute types (R3.7). Kept finite
# (no NaN, allow infinity off) so equality comparisons in assertions are exact.
_scalars = st.one_of(
    st.text(max_size=50),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
)

# Non-scalar values that must be coerced to their str() representation. Mirrors
# the design's ``span_attribute_values()`` strategy (lists, dicts, None, plus
# tuples/sets/objects) extended with a few extra non-scalar shapes.
_non_scalars = st.one_of(
    st.none(),
    st.lists(st.integers(), max_size=5),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=5),
    st.tuples(st.integers(), st.text(max_size=5)),
    st.sets(st.integers(), max_size=5),
    st.builds(object),
    st.complex_numbers(allow_nan=False, allow_infinity=False),
)

# Any attribute value: a mix of scalars and non-scalars (R3.7 input domain).
_attribute_values = st.one_of(_scalars, _non_scalars)

# Attribute keys are ordinary string identifiers passed as kwargs to the helper.
# Restrict to valid Python identifiers since set_attributes takes **kwargs.
_attribute_keys = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,19}", fullmatch=True)

# A dict of arbitrary attribute name -> arbitrary-typed value.
_attribute_dicts = st.dictionaries(_attribute_keys, _attribute_values, max_size=8)

_SCALAR_TYPES = (str, int, float, bool)


def _make_recorder() -> tuple[SpanRecorder, BoundedSpanBuffer]:
    """Build a recorder with an ENABLED sampler and a fresh injected buffer."""
    registry = MetricsRegistry()
    buffer = BoundedSpanBuffer(metrics=registry)
    sampler = TraceSampler(enabled=True, sample_rate=1.0)
    recorder = SpanRecorder(sampler=sampler, span_buffer=buffer, metrics=registry)
    return recorder, buffer


def _fresh_span() -> Span:
    """Build a bare span suitable for direct set_attributes calls."""
    return Span(
        span_id="span",
        parent_span_id=None,
        operation="op",
        start_ts=datetime.now(timezone.utc),
        duration_ms=0,
        status="success",
        attributes={},
    )


# ---------------------------------------------------------------------------
# Property 5 - span attribute values are always scalar.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 5: Span attribute values are always scalar
# Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7
@settings(max_examples=100)
@given(attributes=_attribute_dicts)
def test_set_attributes_coerces_every_value_to_scalar(
    attributes: dict[str, object],
) -> None:
    """Every recorded attribute value is a scalar; non-scalars become str().

    For an arbitrary dict of attribute values of arbitrary Python types,
    ``set_attributes`` records each value such that (R3.7):

    - the stored value is an instance of ``(str, int, float, bool)``;
    - a natively scalar input is preserved with its exact type and value;
    - a non-scalar input is recorded as its ``str()`` representation.
    """
    recorder, _ = _make_recorder()
    span = _fresh_span()

    recorder.set_attributes(span, **attributes)

    assert set(span.attributes) == set(attributes)
    for key, original in attributes.items():
        recorded = span.attributes[key]
        # Every recorded value is a permitted scalar type.
        assert isinstance(recorded, _SCALAR_TYPES)

        if isinstance(original, _SCALAR_TYPES):
            # Native scalars pass through with their type and value intact.
            # (bool is checked before int since bool is a subclass of int.)
            assert type(recorded) is type(original)
            assert recorded == original
        else:
            # Non-scalar inputs are recorded as their str() representation.
            assert recorded == str(original)
            assert type(recorded) is str


# Feature: ai-observability-platform, Property 5: Span attribute values are always scalar
# Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7
@settings(max_examples=100)
@given(attributes=_attribute_dicts)
def test_record_span_routes_attributes_through_scalar_coercion(
    attributes: dict[str, object],
) -> None:
    """record_span enqueues a span whose every attribute value is scalar (R3.7)."""
    recorder, buffer = _make_recorder()

    with recorder.start_trace(trace_id=None, route="route"):
        with recorder.record_span("stage", **attributes):
            pass

    drained = buffer.drain()
    child_spans = [s for s in drained if s.parent_span_id is not None]
    assert len(child_spans) == 1
    child = child_spans[0]

    for key in attributes:
        assert isinstance(child.attributes[key], _SCALAR_TYPES)
    for key, original in attributes.items():
        recorded = child.attributes[key]
        if isinstance(original, _SCALAR_TYPES):
            assert type(recorded) is type(original)
            assert recorded == original
        else:
            assert recorded == str(original)


# Feature: ai-observability-platform, Property 5: Span attribute values are always scalar
# Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7
@settings(max_examples=100)
@given(
    model_id=st.one_of(st.none(), st.text(max_size=40)),
    prompt_tokens=st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
    completion_tokens=st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
    total_tokens=st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
)
def test_generation_attributes_recorded_with_scalar_types(
    model_id: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
) -> None:
    """Generation/routing attributes: model_id str, token counts int (R3.1, R3.7).

    Absent (None) values are recorded with the UNAVAILABLE sentinel string; all
    recorded values are scalars.
    """
    recorder, _ = _make_recorder()
    span = _fresh_span()

    recorder.set_generation_attributes(
        span,
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )

    for value in span.attributes.values():
        assert isinstance(value, _SCALAR_TYPES)

    assert span.attributes["model_id"] == (model_id if model_id is not None else UNAVAILABLE)
    assert isinstance(span.attributes["model_id"], str)
    for key, supplied in (
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
        ("total_tokens", total_tokens),
    ):
        if supplied is None:
            assert span.attributes[key] == UNAVAILABLE
        else:
            assert span.attributes[key] == supplied
            assert isinstance(span.attributes[key], int)


# Feature: ai-observability-platform, Property 5: Span attribute values are always scalar
# Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7
@settings(max_examples=100)
@given(
    retrieval_mode=st.text(max_size=20),
    hit_count=st.integers(min_value=0, max_value=10000),
    top_score=st.one_of(
        st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.integers(),
    ),
)
def test_retrieval_attributes_recorded_with_scalar_types(
    retrieval_mode: str,
    hit_count: int,
    top_score: float | int | None,
) -> None:
    """Retrieval attributes: mode str, hit_count int, top_score number-or-sentinel.

    When hit_count == 0 or no score is supplied, top_score is the NO_SCORE
    sentinel string; otherwise it is the supplied number. All values scalar
    (R3.3, R3.4, R3.7).
    """
    recorder, _ = _make_recorder()
    span = _fresh_span()

    recorder.set_retrieval_attributes(
        span,
        retrieval_mode=retrieval_mode,
        hit_count=hit_count,
        top_score=top_score,
    )

    for value in span.attributes.values():
        assert isinstance(value, _SCALAR_TYPES)

    assert span.attributes["retrieval_mode"] == retrieval_mode
    assert isinstance(span.attributes["retrieval_mode"], str)
    assert span.attributes["hit_count"] == hit_count
    assert isinstance(span.attributes["hit_count"], int)

    recorded_score = span.attributes["top_score"]
    if hit_count == 0 or top_score is None:
        assert recorded_score == NO_SCORE
        assert isinstance(recorded_score, str)
    else:
        assert recorded_score == top_score
        assert isinstance(recorded_score, (int, float))


# Feature: ai-observability-platform, Property 5: Span attribute values are always scalar
# Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7
@settings(max_examples=100)
@given(
    evidence_status=st.text(max_size=20),
    citation_count=st.integers(min_value=0, max_value=10000),
)
def test_answer_generation_attributes_recorded_with_scalar_types(
    evidence_status: str,
    citation_count: int,
) -> None:
    """Answer-generation attributes: evidence_status str, citation_count int (R3.5)."""
    recorder, _ = _make_recorder()
    span = _fresh_span()

    recorder.set_answer_generation_attributes(
        span,
        evidence_status=evidence_status,
        citation_count=citation_count,
    )

    for value in span.attributes.values():
        assert isinstance(value, _SCALAR_TYPES)

    assert span.attributes["evidence_status"] == evidence_status
    assert isinstance(span.attributes["evidence_status"], str)
    assert span.attributes["citation_count"] == citation_count
    assert isinstance(span.attributes["citation_count"], int)


# A document identifier as produced by a pipeline stage: a real string id, an
# absent id (None), or an invalid/non-string object. These are the values a
# stage actually emits — exercising R3.6's "regardless of validity" clause.
_document_ids = st.one_of(
    st.text(max_size=40),
    st.none(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
    st.builds(object),
)


# Feature: ai-observability-platform, Property 5: Span attribute values are always scalar
# Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7
@settings(max_examples=100)
@given(document_id=_document_ids)
def test_document_id_recorded_as_string_regardless_of_validity(
    document_id: object,
) -> None:
    """document_id is always recorded as a string scalar, valid or not (R3.6, R3.7).

    A real string id is preserved verbatim; an absent (None) or invalid
    (non-string object) id is recorded as its ``str()`` representation. In every
    case the stored value is a string, so a document id is always a string Span
    attribute regardless of the document's validity.
    """
    recorder, _ = _make_recorder()
    span = _fresh_span()

    recorder.set_document_id(span, document_id)

    recorded = span.attributes["document_id"]
    assert isinstance(recorded, str)
    if isinstance(document_id, str):
        assert recorded == document_id
    else:
        assert recorded == str(document_id)
