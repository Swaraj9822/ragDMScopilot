"""Property test for ingestion stage spans.

Feature: ai-observability-platform, Property 27: Ingestion produces exactly one child span per stage with stage attributes

This module validates that the SpanRecorder, when used as the ingestion pipeline
instruments it, creates exactly one child span per ingestion stage (parsing,
chunking, embedding, indexing) as direct children of the root span, each carrying
the document_id and document_version attributes.

Requirements covered:

* R12.2 — WHEN each ingestion stage runs, THE Span_Recorder SHALL create exactly
  one child Span per stage for parsing, chunking, embedding, and indexing, each
  as a direct child of the Root_Span.
* R12.4 — WHEN an ingestion stage completes, THE Span_Recorder SHALL record the
  document identifier and document version as attributes of that stage's child
  Span.
"""

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Strategies — constrained to realistic document identity domain.
# ---------------------------------------------------------------------------

# document_id: non-empty printable strings (simulating UUIDs or slug ids).
_document_ids = st.text(
    alphabet=string.ascii_letters + string.digits + "-_",
    min_size=1,
    max_size=64,
)

# document_version: non-empty version strings (e.g. "v1", "20240101", short hashes).
_document_versions = st.text(
    alphabet=string.ascii_letters + string.digits + ".-_",
    min_size=1,
    max_size=32,
)

# The four ingestion stage operation names as used in the production pipeline.
INGESTION_STAGES = ["document parsing", "chunking", "dense embedding", "Pinecone upsert"]


# ---------------------------------------------------------------------------
# Property 27 — Ingestion produces exactly one child span per stage with
# stage attributes.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 27: Ingestion produces exactly one child span per stage with stage attributes
# Validates: Requirements 12.2, 12.4
@settings(max_examples=100)
@given(
    document_id=_document_ids,
    document_version=_document_versions,
)
def test_ingestion_produces_one_child_span_per_stage_with_attributes(
    document_id: str,
    document_version: str,
) -> None:
    """Each ingestion run produces exactly 4 child spans with correct attributes.

    R12.2: Exactly one child span per stage (parsing, chunking, embedding,
    indexing), each as a direct child of the root span.
    R12.4: Each stage span carries document_id and document_version attributes
    matching the ingested document.
    """

    # Set up a fresh recorder with sampling enabled (rate=1.0) so all traces
    # are captured.
    metrics = MetricsRegistry()
    buffer = BoundedSpanBuffer(metrics=metrics)
    sampler = TraceSampler(enabled=True, sample_rate=1.0)
    recorder = SpanRecorder(sampler=sampler, span_buffer=buffer, metrics=metrics)

    # Simulate what the instrumented ingestion pipeline does:
    # 1. Open a root trace (as the ingestion worker does).
    # 2. Inside the trace, open one child span per stage and set attributes.
    with recorder.start_trace(trace_id=None, route="ingestion", is_root_http=False):
        for stage_name in INGESTION_STAGES:
            with recorder.record_span(stage_name) as span:
                recorder.set_ingestion_attributes(
                    span,
                    document_id=document_id,
                    document_version=document_version,
                )

    # Drain all completed spans from the buffer.
    spans = buffer.drain()

    # The buffer should contain: 1 root span + 4 stage child spans = 5 total.
    assert len(spans) == 5, (
        f"Expected 5 spans (1 root + 4 stages), got {len(spans)}"
    )

    # Identify the root span (parent_span_id is None).
    root_spans = [s for s in spans if s.parent_span_id is None]
    assert len(root_spans) == 1, (
        f"Expected exactly 1 root span, got {len(root_spans)}"
    )
    root_span = root_spans[0]

    # Identify child spans (parent_span_id equals the root's span_id).
    child_spans = [s for s in spans if s.parent_span_id == root_span.span_id]
    assert len(child_spans) == 4, (
        f"Expected exactly 4 child spans (one per stage), got {len(child_spans)}"
    )

    # Verify each stage is represented exactly once and has correct attributes.
    child_operations = [s.operation for s in child_spans]
    for stage_name in INGESTION_STAGES:
        count = child_operations.count(stage_name)
        assert count == 1, (
            f"Expected exactly 1 span with operation={stage_name!r}, "
            f"got {count}. All operations: {child_operations}"
        )

    # Verify each child span has the correct document_id and document_version.
    for child in child_spans:
        assert child.attributes.get("document_id") == document_id, (
            f"Span {child.operation!r} should have document_id={document_id!r}, "
            f"got {child.attributes.get('document_id')!r}"
        )
        assert child.attributes.get("document_version") == document_version, (
            f"Span {child.operation!r} should have document_version={document_version!r}, "
            f"got {child.attributes.get('document_version')!r}"
        )
