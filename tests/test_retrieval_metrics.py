"""Unit tests for `compute_retrieval_metrics` (R7.2, R7.9)."""

from __future__ import annotations

from rag_system.models import QueryTraceHit, RelevanceLabels
from rag_system.retrieval_metrics import (
    compute_retrieval_metrics,
    relevant_identifiers,
)


def _hit(chunk_id: str, document_id: str, score: float) -> QueryTraceHit:
    return QueryTraceHit(
        chunk_id=chunk_id,
        document_id=document_id,
        version="v1",
        score=score,
        source="s",
        text="t",
    )


def test_absent_labels_skip_metrics() -> None:
    hits = [_hit("c1", "d1", 0.9)]
    assert compute_retrieval_metrics(hits, None, depth=10) is None


def test_labels_present_but_empty_yields_zero_metrics() -> None:
    hits = [_hit("c1", "d1", 0.9)]
    metrics = compute_retrieval_metrics(hits, RelevanceLabels(), depth=10)
    assert metrics is not None
    assert metrics.recall_at_k == 0.0
    assert metrics.precision_at_k == 0.0
    assert metrics.mrr_at_k == 0.0
    assert metrics.depth == 10


def test_empty_retrieval_yields_zero_metrics() -> None:
    labels = RelevanceLabels(relevant_chunk_ids=["c1"])
    metrics = compute_retrieval_metrics([], labels, depth=10)
    assert metrics is not None
    assert metrics.recall_at_k == 0.0
    assert metrics.precision_at_k == 0.0
    assert metrics.mrr_at_k == 0.0


def test_perfect_retrieval() -> None:
    hits = [_hit("c1", "d1", 0.9), _hit("c2", "d1", 0.8)]
    labels = RelevanceLabels(relevant_chunk_ids=["c1", "c2"])
    metrics = compute_retrieval_metrics(hits, labels, depth=10)
    assert metrics is not None
    assert metrics.recall_at_k == 1.0
    assert metrics.precision_at_k == 1.0
    assert metrics.mrr_at_k == 1.0


def test_mrr_uses_first_relevant_rank_by_score() -> None:
    # Highest score is irrelevant; the second-ranked hit is the first relevant.
    hits = [_hit("c-irrelevant", "d9", 0.95), _hit("c1", "d1", 0.90)]
    labels = RelevanceLabels(relevant_chunk_ids=["c1"])
    metrics = compute_retrieval_metrics(hits, labels, depth=10)
    assert metrics is not None
    assert metrics.mrr_at_k == 0.5
    assert metrics.precision_at_k == 0.5
    assert metrics.recall_at_k == 1.0


def test_depth_truncates_to_top_k() -> None:
    # Relevant hit sits at rank 3 but depth is 2, so it is excluded.
    hits = [
        _hit("c-a", "d-a", 0.9),
        _hit("c-b", "d-b", 0.8),
        _hit("c1", "d1", 0.7),
    ]
    labels = RelevanceLabels(relevant_chunk_ids=["c1"])
    metrics = compute_retrieval_metrics(hits, labels, depth=2)
    assert metrics is not None
    assert metrics.recall_at_k == 0.0
    assert metrics.precision_at_k == 0.0
    assert metrics.mrr_at_k == 0.0
    assert metrics.depth == 2


def test_document_level_and_human_judgment_labels_match() -> None:
    hits = [_hit("c1", "d1", 0.9), _hit("c2", "d2", 0.8)]
    labels = RelevanceLabels(
        relevant_document_ids=["d1"],
        human_judgments={"d2": 0.75, "d3": 0.0},
    )
    # d1 (document label) and d2 (positive human judgment) are relevant; d3 is not.
    assert relevant_identifiers(labels) == {"d1", "d2"}
    metrics = compute_retrieval_metrics(hits, labels, depth=10)
    assert metrics is not None
    assert metrics.recall_at_k == 1.0
    assert metrics.precision_at_k == 1.0
    assert metrics.mrr_at_k == 1.0


def test_metrics_within_unit_interval_partial_match() -> None:
    hits = [_hit("c1", "d1", 0.9), _hit("c-x", "d-x", 0.5)]
    labels = RelevanceLabels(relevant_chunk_ids=["c1", "c2", "c3"])
    metrics = compute_retrieval_metrics(hits, labels, depth=10)
    assert metrics is not None
    # 1 of 3 relevant ids matched.
    assert metrics.recall_at_k == 1 / 3
    # 1 of 2 retrieved hits relevant.
    assert metrics.precision_at_k == 0.5
    assert metrics.mrr_at_k == 1.0
    for value in (metrics.recall_at_k, metrics.precision_at_k, metrics.mrr_at_k):
        assert 0.0 <= value <= 1.0
