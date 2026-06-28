"""Property test for enqueue-to-worker trace association.

Feature: ai-observability-platform, Property 29: Enqueue-to-worker trace association is preserved

This module validates the end-to-end trace association chain between the enqueue
site and the ingestion worker:

- R2.7 — WHEN an ingestion job is enqueued while a Trace is active, THE
  Tracing_Platform SHALL include the active trace_id in the job payload.
- R2.9 — WHEN the worker processes an ingestion job whose payload carries a
  non-null trace_id, THE Tracing_Platform SHALL associate the resulting
  Ingestion_Trace with that trace_id.
- R2.10 — IF an ingestion job carries a null or absent trace_id, THEN THE
  Tracing_Platform SHALL generate a new trace_id for the Ingestion_Trace as an
  independent Trace with no parent linkage.

The test exercises three levels:
1. The enqueue site (service creates IngestionJob with the active trace_id).
2. The SpanRecorder's ``start_trace`` with a non-null trace_id (adoption).
3. The SpanRecorder's ``start_trace`` with a None trace_id (generation).
"""

import re
import string

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import (
    MetricsRegistry,
    reset_trace_id,
    set_trace_id,
)
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.context import get_active_trace_id
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler
from rag_system.queue import IngestionJob

# ---------------------------------------------------------------------------
# Smart generators — constrained to realistic trace/span identity domain.
# ---------------------------------------------------------------------------

# trace_id: 32-char lowercase hex strings (matching the format the recorder
# produces and the format expected on the wire).
_trace_ids = st.text(
    alphabet=string.hexdigits[:16],  # lowercase hex chars
    min_size=32,
    max_size=32,
)

# document_id / version / filename / s3_uri for IngestionJob construction.
_document_ids = st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=20)
_versions = st.text(alphabet=string.digits + ".", min_size=1, max_size=10)
_filenames = st.text(alphabet=string.ascii_lowercase + ".", min_size=3, max_size=20)
_s3_uris = st.text(alphabet=string.ascii_lowercase + string.digits + "/-", min_size=5, max_size=40)

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
# Property 29 — Enqueue-to-worker trace association is preserved.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 29: Enqueue-to-worker trace association is preserved
# Validates: Requirements 2.7
@settings(max_examples=100)
@given(
    trace_id=_trace_ids,
    document_id=_document_ids,
    version=_versions,
    filename=_filenames,
    s3_uri=_s3_uris,
)
def test_enqueue_captures_active_trace_id_in_job_payload(
    trace_id: str,
    document_id: str,
    version: str,
    filename: str,
    s3_uri: str,
) -> None:
    """When a trace is active at enqueue time, the job payload carries it (R2.7).

    The service creates an ``IngestionJob`` with ``trace_id=get_active_trace_id()``
    when enqueueing. This test validates that setting a trace_id in context and
    then constructing an IngestionJob using ``get_active_trace_id()`` produces a
    job whose ``trace_id`` matches the active one exactly.
    """
    # Simulate an active trace on the request path.
    token = set_trace_id(trace_id)
    try:
        # This mirrors what service.py does:
        #   job = IngestionJob(..., trace_id=get_active_trace_id())
        captured_trace_id = get_active_trace_id()
        job = IngestionJob(
            document_id=document_id,
            version=version,
            filename=filename,
            s3_uri=s3_uri,
            trace_id=captured_trace_id,
        )
        # The job payload must carry the active trace_id verbatim.
        assert job.trace_id == trace_id, (
            f"Expected job.trace_id={trace_id!r}, got {job.trace_id!r}"
        )
    finally:
        reset_trace_id(token)


# Feature: ai-observability-platform, Property 29: Enqueue-to-worker trace association is preserved
# Validates: Requirements 2.7
@settings(max_examples=100)
@given(
    document_id=_document_ids,
    version=_versions,
    filename=_filenames,
    s3_uri=_s3_uris,
)
def test_enqueue_with_no_active_trace_produces_null_trace_id(
    document_id: str,
    version: str,
    filename: str,
    s3_uri: str,
) -> None:
    """When no trace is active at enqueue time, the job carries null trace_id (R2.7/R2.8).

    This ensures that when there is no active trace context, the IngestionJob's
    trace_id is None, which later causes the worker to generate a new independent
    trace_id (R2.10).
    """
    # Ensure no trace is active (default state of the ContextVar).
    token = set_trace_id(None)
    try:
        captured_trace_id = get_active_trace_id()
        job = IngestionJob(
            document_id=document_id,
            version=version,
            filename=filename,
            s3_uri=s3_uri,
            trace_id=captured_trace_id,
        )
        assert job.trace_id is None, (
            f"Expected job.trace_id=None when no trace active, got {job.trace_id!r}"
        )
    finally:
        reset_trace_id(token)


# Feature: ai-observability-platform, Property 29: Enqueue-to-worker trace association is preserved
# Validates: Requirements 2.9
@settings(max_examples=100)
@given(trace_id=_trace_ids)
def test_worker_start_trace_adopts_payload_trace_id(trace_id: str) -> None:
    """When job.trace_id is non-null, the worker's start_trace adopts it (R2.9).

    The worker calls ``recorder.start_trace(trace_id=job.trace_id, ...)``. When
    that trace_id is non-null, the resulting root span and trace context must use
    that exact trace_id.
    """
    recorder, span_buffer = _make_recorder()

    # Simulate what the worker does: start_trace with the job's trace_id.
    with recorder.start_trace(trace_id=trace_id, route="ingestion", is_root_http=False) as root:
        # The active trace_id inside the block must be the payload trace_id.
        active = get_active_trace_id()
        assert active == trace_id, (
            f"Expected active trace_id={trace_id!r}, got {active!r}"
        )
        # The root span must be a root (no parent).
        assert root.parent_span_id is None

    # The enqueued root span should reflect the adopted trace_id.
    drained = span_buffer.drain()
    assert len(drained) == 1
    assert drained[0].parent_span_id is None
    assert drained[0].span_id == root.span_id


# Feature: ai-observability-platform, Property 29: Enqueue-to-worker trace association is preserved
# Validates: Requirements 2.10
@settings(max_examples=100)
@given(data=st.data())
def test_worker_start_trace_generates_independent_trace_id_when_null(
    data: st.DataObject,
) -> None:
    """When job.trace_id is None, the worker generates a new independent trace_id (R2.10).

    The worker calls ``recorder.start_trace(trace_id=None, ...)``. The recorder
    must generate a new 32-char hex trace_id that is unique and has no parent
    linkage. Each invocation must produce a different trace_id.
    """
    recorder, span_buffer = _make_recorder()
    num_traces = data.draw(st.integers(min_value=2, max_value=5), label="num_traces")

    generated_ids: list[str] = []

    for _ in range(num_traces):
        with recorder.start_trace(trace_id=None, route="ingestion", is_root_http=False) as root:
            active = get_active_trace_id()
            assert active is not None, "A trace_id should be generated"
            assert _HEX32.match(active), (
                f"Generated trace_id must be 32-char hex, got {active!r}"
            )
            assert root.parent_span_id is None, (
                "Root span should have no parent (independent trace)"
            )
            generated_ids.append(active)

    # All generated trace_ids must be unique (independent traces).
    assert len(set(generated_ids)) == num_traces, (
        f"Expected {num_traces} unique trace_ids, got {len(set(generated_ids))}: "
        f"{generated_ids}"
    )

    # The buffer should contain one root span per trace.
    drained = span_buffer.drain()
    assert len(drained) == num_traces
    for span in drained:
        assert span.parent_span_id is None
