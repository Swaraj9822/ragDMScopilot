"""Property tests for ingestion failure marking.

Feature: ai-observability-platform, Property 28: Ingestion failure marks the failing stage and root as error

These tests exercise the :class:`~rag_system.observability_tracing.recorder.SpanRecorder`
to verify that when an exception occurs inside a ``record_span`` within an open
``start_trace`` block (simulating an ingestion job with multiple stages), the
invariants specified by R12.6 hold:

1. The failing stage's span has status ``"error"``.
2. The failing stage's span carries ``exception.type`` and ``exception.message``
   attributes.
3. The root span has status ``"error"`` (because the exception propagates out of
   the trace block).
4. Stages that completed successfully before the failure have status ``"success"``.

**Validates: Requirements 12.6**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators — constrained to the recorder's input domain.
# ---------------------------------------------------------------------------

# Operation labels for ingestion stages.
_INGESTION_STAGES = ["parsing", "chunking", "embedding", "indexing"]

# Number of total stages: between 1 and 4.
_num_stages = st.integers(min_value=1, max_value=4)

# The exception types that an ingestion stage might raise.
_EXC_TYPES = [
    ValueError,
    KeyError,
    RuntimeError,
    TypeError,
    OSError,
    IOError,
    Exception,
]

# Random error messages.
_error_messages = st.text(min_size=1, max_size=200)

# Random document ids and versions for realistic ingestion context.
_document_ids = st.text(min_size=1, max_size=40, alphabet=st.characters(whitelist_categories=("L", "N", "P")))
_versions = st.integers(min_value=1, max_value=1000)


@st.composite
def _ingestion_failure_scenario(draw: st.DrawFn) -> dict:
    """Build an ingestion scenario with a randomly chosen failing stage.

    Returns a dict with:
    - num_stages: total number of stages (1..4)
    - fail_index: the 0-based index of the failing stage (0..num_stages-1)
    - operations: list of operation labels for each stage
    - exc_type: the type of exception raised by the failing stage
    - error_message: the message for the exception
    - document_id: a random document identifier
    - version: a random document version
    """
    num = draw(_num_stages)
    fail_index = draw(st.integers(min_value=0, max_value=num - 1))
    operations = [_INGESTION_STAGES[i % len(_INGESTION_STAGES)] for i in range(num)]
    exc_type = draw(st.sampled_from(_EXC_TYPES))
    error_message = draw(_error_messages)
    document_id = draw(_document_ids)
    version = draw(_versions)
    return {
        "num_stages": num,
        "fail_index": fail_index,
        "operations": operations,
        "exc_type": exc_type,
        "error_message": error_message,
        "document_id": document_id,
        "version": version,
    }


def _make_recorder() -> tuple[SpanRecorder, BoundedSpanBuffer]:
    """Build a recorder with an ENABLED sampler and a fresh injected buffer."""
    registry = MetricsRegistry()
    buffer = BoundedSpanBuffer(metrics=registry)
    sampler = TraceSampler(enabled=True, sample_rate=1.0)
    recorder = SpanRecorder(sampler=sampler, span_buffer=buffer, metrics=registry)
    return recorder, buffer


# ---------------------------------------------------------------------------
# Property 28 — Ingestion failure marks the failing stage and root as error.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 28: Ingestion failure marks the failing stage and root as error
# Validates: Requirements 12.6
@settings(max_examples=100)
@given(scenario=_ingestion_failure_scenario())
def test_ingestion_failure_marks_failing_stage_and_root_as_error(
    scenario: dict,
) -> None:
    """When an exception occurs in a stage, the failing stage and root span are error.

    For an arbitrary number of ingestion stages (1..4) and a randomly chosen
    failing stage index, running ``record_span`` blocks inside a ``start_trace``
    block and raising an exception in the failing stage enqueues spans such that:

    - All stages that completed before the failure have status ``"success"``.
    - The failing stage span has status ``"error"``.
    - The failing stage span has ``exception.type`` and ``exception.message`` attributes.
    - The root span has status ``"error"`` because the exception propagated out.
    """
    recorder, buffer = _make_recorder()

    num_stages = scenario["num_stages"]
    fail_index = scenario["fail_index"]
    operations = scenario["operations"]
    exc_type = scenario["exc_type"]
    error_message = scenario["error_message"]

    exc = exc_type(error_message)

    # Run the ingestion simulation: start a trace, open stages, fail at fail_index.
    with pytest.raises(type(exc)):
        with recorder.start_trace(trace_id=None, route="ingestion", is_root_http=False):
            for i in range(num_stages):
                if i == fail_index:
                    # This stage raises — the exception will propagate through
                    # record_span (which records error status) and then through
                    # start_trace (which records error on the root span).
                    with recorder.record_span(operations[i]):
                        raise exc
                else:
                    # Successful stages before the failure.
                    with recorder.record_span(operations[i]):
                        pass

    # Drain all enqueued spans.
    drained = buffer.drain()

    # Identify the root span (parent_span_id is None).
    root_spans = [s for s in drained if s.parent_span_id is None]
    assert len(root_spans) == 1, "expected exactly one root span"
    root_span = root_spans[0]

    # Identify child spans (parent_span_id is not None), ordered by their
    # position in the drained list (which reflects insertion/completion order).
    child_spans = [s for s in drained if s.parent_span_id is not None]

    # We expect exactly (fail_index + 1) child spans: the stages that ran
    # before the failure plus the failing stage itself. Stages after the failure
    # are never started.
    assert len(child_spans) == fail_index + 1, (
        f"expected {fail_index + 1} child spans, got {len(child_spans)}"
    )

    # Successful stages (all before the failing one) have status "success".
    for i in range(fail_index):
        assert child_spans[i].status == "success", (
            f"stage {i} should be 'success' but got '{child_spans[i].status}'"
        )

    # The failing stage has status "error".
    failing_span = child_spans[fail_index]
    assert failing_span.status == "error"

    # The failing stage has exception attributes (R12.6).
    assert "exception.type" in failing_span.attributes
    assert "exception.message" in failing_span.attributes
    assert failing_span.attributes["exception.type"] == exc_type.__name__
    # The recorder stores str(exc)[:4096]; some exception types (e.g. KeyError)
    # format their message differently from the raw string passed to them.
    expected_message = str(exc)[:4096]
    assert failing_span.attributes["exception.message"] == expected_message

    # The root span has status "error" because the exception propagated out (R12.6).
    assert root_span.status == "error"
