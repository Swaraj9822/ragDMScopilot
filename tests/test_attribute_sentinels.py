"""Unit tests for the attribute-sentinel behaviour of the Span_Recorder.

Feature: ai-observability-platform. Task 4.8.

These example-based tests pin down the two sentinel paths in the stage-specific
attribute helpers of
:class:`rag_system.observability_tracing.recorder.SpanRecorder`:

- Generation / routing spans: when the LLM provider does not return a model id
  or one or more token counts, each absent value's attribute is recorded with
  the explicit :data:`UNAVAILABLE` sentinel, every available value is still
  recorded with its real value, and no error is raised (R3.2).
- Retrieval spans: when ``hit_count == 0`` (or no score is available), the
  top-retrieval-score attribute is recorded with the explicit :data:`NO_SCORE`
  sentinel rather than a number; with a positive hit count and a real score the
  numeric score is recorded (R3.4).

The helpers operate directly on a :class:`Span`'s ``attributes`` dict, so each
test builds a fresh span, invokes the helper on a recorder instance, and
inspects ``span.attributes``.
"""

from datetime import datetime, timezone

import pytest

from rag_system.observability_tracing.models import Span
from rag_system.observability_tracing.recorder import (
    NO_SCORE,
    UNAVAILABLE,
    SpanRecorder,
)


def _make_span() -> Span:
    """Build a fresh, empty Span suitable for annotating in a test."""
    return Span(
        span_id="span-under-test",
        parent_span_id=None,
        operation="op",
        start_ts=datetime.now(timezone.utc),
        duration_ms=0,
        status="success",
        attributes={},
    )


def _make_recorder() -> SpanRecorder:
    """Build a recorder for exercising the attribute helpers.

    The attribute helpers only manipulate the supplied span's ``attributes``
    dict; they never consult the sampler, propagator, buffer, or metrics, so a
    bare recorder with a ``None`` sampler is sufficient here.
    """
    return SpanRecorder(sampler=None)


# ---------------------------------------------------------------------------
# R3.2 - generation/routing missing model id and token counts -> UNAVAILABLE.
# ---------------------------------------------------------------------------


# Validates: Requirements 3.2
def test_generation_all_attributes_missing_recorded_as_unavailable() -> None:
    """With every value absent, all four attributes are the UNAVAILABLE sentinel.

    No exception is raised even though the provider returned nothing (R3.2).
    """
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_generation_attributes(span)

    assert span.attributes["model_id"] == UNAVAILABLE
    assert span.attributes["prompt_tokens"] == UNAVAILABLE
    assert span.attributes["completion_tokens"] == UNAVAILABLE
    assert span.attributes["total_tokens"] == UNAVAILABLE


# Validates: Requirements 3.2
def test_generation_partial_missing_mixes_real_values_and_sentinel() -> None:
    """Provided values are recorded as-is; only the absent ones use the sentinel.

    Here the model id and prompt token count are supplied while the completion
    and total token counts are absent (R3.2).
    """
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_generation_attributes(
        span,
        model_id="gemini-1.5-pro",
        prompt_tokens=128,
    )

    # Available values recorded with their real values and native types.
    assert span.attributes["model_id"] == "gemini-1.5-pro"
    assert span.attributes["prompt_tokens"] == 128
    assert isinstance(span.attributes["prompt_tokens"], int)

    # Absent values recorded with the explicit sentinel.
    assert span.attributes["completion_tokens"] == UNAVAILABLE
    assert span.attributes["total_tokens"] == UNAVAILABLE


# Validates: Requirements 3.2
def test_generation_only_model_missing_keeps_token_counts() -> None:
    """A missing model id alone does not affect the recorded token counts (R3.2)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_generation_attributes(
        span,
        model_id=None,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
    )

    assert span.attributes["model_id"] == UNAVAILABLE
    assert span.attributes["prompt_tokens"] == 10
    assert span.attributes["completion_tokens"] == 20
    assert span.attributes["total_tokens"] == 30


# Validates: Requirements 3.2
def test_generation_all_present_records_real_values() -> None:
    """When everything is supplied, no sentinel appears (R3.2 negative case)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_generation_attributes(
        span,
        model_id="gpt-4o",
        prompt_tokens=0,
        completion_tokens=5,
        total_tokens=5,
    )

    assert span.attributes == {
        "model_id": "gpt-4o",
        "prompt_tokens": 0,
        "completion_tokens": 5,
        "total_tokens": 5,
    }
    assert UNAVAILABLE not in span.attributes.values()


# Validates: Requirements 3.2
def test_generation_missing_attributes_does_not_raise() -> None:
    """Recording with all values absent completes without raising (R3.2)."""
    recorder = _make_recorder()
    span = _make_span()

    try:
        recorder.set_generation_attributes(span)
    except Exception as exc:  # pragma: no cover - failure path
        pytest.fail(f"set_generation_attributes raised unexpectedly: {exc!r}")


# Validates: Requirements 3.2
def test_routing_alias_applies_same_sentinel_behaviour() -> None:
    """Routing spans share the generation helper, so the sentinels match (R3.2)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_routing_attributes(span, model_id="router-v2")

    assert span.attributes["model_id"] == "router-v2"
    assert span.attributes["prompt_tokens"] == UNAVAILABLE
    assert span.attributes["completion_tokens"] == UNAVAILABLE
    assert span.attributes["total_tokens"] == UNAVAILABLE


# ---------------------------------------------------------------------------
# R3.4 - retrieval hit_count == 0 (or no score) -> NO_SCORE sentinel.
# ---------------------------------------------------------------------------


# Validates: Requirements 3.4
def test_retrieval_zero_hits_records_no_score_sentinel() -> None:
    """A hit count of 0 records the top score as the NO_SCORE sentinel (R3.4)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_retrieval_attributes(span, retrieval_mode="hybrid", hit_count=0)

    assert span.attributes["retrieval_mode"] == "hybrid"
    assert span.attributes["hit_count"] == 0
    assert span.attributes["top_score"] == NO_SCORE


# Validates: Requirements 3.4
def test_retrieval_zero_hits_ignores_supplied_score() -> None:
    """Even if a score is supplied, 0 hits still records NO_SCORE (R3.4)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_retrieval_attributes(
        span, retrieval_mode="dense", hit_count=0, top_score=0.99
    )

    assert span.attributes["top_score"] == NO_SCORE


# Validates: Requirements 3.4
def test_retrieval_positive_hits_records_numeric_score() -> None:
    """With hits and a real score, the numeric top score is recorded (R3.4)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_retrieval_attributes(
        span, retrieval_mode="dense", hit_count=3, top_score=0.873
    )

    assert span.attributes["retrieval_mode"] == "dense"
    assert span.attributes["hit_count"] == 3
    assert span.attributes["top_score"] == 0.873
    assert isinstance(span.attributes["top_score"], float)


# Validates: Requirements 3.4
def test_retrieval_positive_hits_but_no_score_records_no_score_sentinel() -> None:
    """A positive hit count with no available score still uses NO_SCORE (R3.4)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_retrieval_attributes(
        span, retrieval_mode="sparse", hit_count=5, top_score=None
    )

    assert span.attributes["hit_count"] == 5
    assert span.attributes["top_score"] == NO_SCORE


# Validates: Requirements 3.4
def test_retrieval_integer_score_recorded_as_is() -> None:
    """An integer top score is recorded with its native value (R3.4)."""
    recorder = _make_recorder()
    span = _make_span()

    recorder.set_retrieval_attributes(
        span, retrieval_mode="dense", hit_count=1, top_score=1
    )

    assert span.attributes["top_score"] == 1
