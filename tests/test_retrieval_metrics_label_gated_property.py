# Feature: rag-trust-and-observability, Property 25: Retrieval metrics are gated on relevance labels and well-formed
"""Property-based test for label-gated retrieval metrics (task 12.4).

Feature: rag-trust-and-observability.

**Property 25: Retrieval metrics are gated on relevance labels and well-formed.**

**Validates: Requirements 7.2, 7.9.**

*For any* ranked retrieval (``hits``), depth ``k``, and optional
``Relevance_Labels``:

* R7.9 — when relevance labels are absent (``None``),
  :func:`compute_retrieval_metrics` computes nothing and returns ``None``
  (the metric method is skipped entirely).
* R7.2 — when relevance labels are present, the function computes recall@k,
  precision@k, and MRR@k at the configured depth. Every metric is a
  well-formed value within ``[0.0, 1.0]`` and the recorded ``depth`` matches
  the (non-negative) depth requested.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from rag_system.models import QueryTraceHit, RelevanceLabels, RetrievalMetrics
from rag_system.retrieval_metrics import compute_retrieval_metrics

# Small, overlapping identifier space so generated hits and labels intersect
# often enough to exercise non-trivial (relevant) outcomes as well as misses.
_IDS = st.sampled_from(["a", "b", "c", "d", "e", "f", "g", "h"])


@st.composite
def _hits(draw: st.DrawFn) -> list[QueryTraceHit]:
    count = draw(st.integers(min_value=0, max_value=12))
    hits: list[QueryTraceHit] = []
    for _ in range(count):
        hits.append(
            QueryTraceHit(
                chunk_id=draw(_IDS),
                document_id=draw(_IDS),
                version="v1",
                score=draw(
                    st.floats(
                        min_value=-1.0,
                        max_value=1.0,
                        allow_nan=False,
                        allow_infinity=False,
                    )
                ),
                source="s",
                text="t",
            )
        )
    return hits


@st.composite
def _labels(draw: st.DrawFn) -> RelevanceLabels:
    return RelevanceLabels(
        relevant_chunk_ids=draw(st.lists(_IDS, max_size=6, unique=True)),
        relevant_document_ids=draw(st.lists(_IDS, max_size=6, unique=True)),
        human_judgments=draw(
            st.dictionaries(
                _IDS,
                st.floats(
                    min_value=-1.0,
                    max_value=1.0,
                    allow_nan=False,
                    allow_infinity=False,
                ),
                max_size=6,
            )
        ),
    )


@given(hits=_hits(), depth=st.integers(min_value=0, max_value=20))
def test_absent_labels_skip_metrics(
    hits: list[QueryTraceHit], depth: int
) -> None:
    # R7.9: no relevance labels => no retrieval metrics computed.
    assert compute_retrieval_metrics(hits, None, depth) is None


@given(
    hits=_hits(),
    labels=_labels(),
    depth=st.integers(min_value=0, max_value=20),
)
def test_present_labels_yield_well_formed_metrics(
    hits: list[QueryTraceHit],
    labels: RelevanceLabels,
    depth: int,
) -> None:
    # R7.2: labels present => metrics computed at the configured depth.
    metrics = compute_retrieval_metrics(hits, labels, depth)

    assert isinstance(metrics, RetrievalMetrics)
    for value in (
        metrics.recall_at_k,
        metrics.precision_at_k,
        metrics.mrr_at_k,
    ):
        assert 0.0 <= value <= 1.0
    # Depth is recorded (clamped to non-negative).
    assert metrics.depth == max(depth, 0)
