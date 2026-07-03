"""Unit tests for the knowledge gap map service (R11).

Feature: rag-trust-and-observability (task 18.1).

Covers eligibility selection joining traces + feedback (R11.1), the
insufficient-outcomes minimum gate (R11.6), bounded clustering and well-formed
topics (R11.1, R11.2), coverage-quality derivation from aggregate signals
(R11.2), the four recommendation categories (R11.4), and the
generation-failure error (R11.5). LLM/embedding steps are stubbed for
determinism.
"""

from __future__ import annotations

import pytest

from rag_system.knowledge_gap import (
    CoverageThresholds,
    EligibleOutcome,
    KnowledgeGapGenerationError,
    generate_knowledge_gap_map,
    select_eligible_outcomes,
)
from rag_system.models import (
    FeedbackReviewRecord,
    KnowledgeGapMap,
    QueryTraceRecord,
    ReasonCode,
)

# Topic keywords used by the deterministic stub embedder: questions sharing a
# keyword embed to the same one-hot vector (cosine 1.0 -> same cluster);
# different keywords embed orthogonally (cosine 0.0 -> distinct clusters).
_TOPIC_KEYWORDS = ["alpha", "beta", "gamma", "delta", "epsilon"]


def _embed(question: str) -> list[float]:
    vector = [0.0] * len(_TOPIC_KEYWORDS)
    for index, keyword in enumerate(_TOPIC_KEYWORDS):
        if keyword in question:
            vector[index] = 1.0
    if sum(vector) == 0.0:
        vector[0] = 1.0
    return vector


def _label(questions: list[str]) -> str:
    # Deterministic label: the shared keyword, else the first question.
    for keyword in _TOPIC_KEYWORDS:
        if questions and all(keyword in q for q in questions):
            return f"topic:{keyword}"
    return questions[0] if questions else "topic"


def _trace(
    trace_id: str,
    question: str,
    *,
    confidence_score: float | None = None,
    abstention_reason_code: ReasonCode | None = None,
    document_ids: list[str] | None = None,
) -> QueryTraceRecord:
    return QueryTraceRecord(
        trace_id=trace_id,
        question=question,
        route="documents",
        answer="an answer",
        evidence_status="supported",
        confidence_score=confidence_score,
        abstention_reason_code=abstention_reason_code,
        document_ids=document_ids,
    )


def _feedback(trace_id: str, *, rating: int) -> FeedbackReviewRecord:
    return FeedbackReviewRecord(
        rating=rating,
        trace_id=trace_id,
        feedback_id=f"fb-{trace_id}",
        created_at="2024-01-01T00:00:00Z",
    )


def _outcome(trace_id: str, question: str, **kwargs) -> EligibleOutcome:
    return EligibleOutcome(trace_id=trace_id, question=question, **kwargs)


def _generate(outcomes, *, max_topics=25, min_eligible=1, thresholds=None):
    return generate_knowledge_gap_map(
        outcomes,
        embed_question=_embed,
        label_cluster=_label,
        max_topics=max_topics,
        min_eligible_outcomes=min_eligible,
        thresholds=thresholds,
    )


# ---------------------------------------------------------------------------
# Eligibility selection (R11.1)
# ---------------------------------------------------------------------------


def test_select_low_confidence_abstained_and_negatively_rated() -> None:
    traces = [
        _trace("t-low", "alpha question", confidence_score=0.2),
        _trace("t-abstain", "beta question", abstention_reason_code=ReasonCode.no_evidence),
        _trace("t-neg", "gamma question", confidence_score=0.9),
        _trace("t-fine", "delta question", confidence_score=0.9),
    ]
    feedback = [_feedback("t-neg", rating=1)]

    outcomes = select_eligible_outcomes(traces, feedback, confidence_threshold=0.5)
    by_id = {o.trace_id: o for o in outcomes}

    assert set(by_id) == {"t-low", "t-abstain", "t-neg"}
    assert by_id["t-low"].low_confidence is True
    assert by_id["t-abstain"].abstained is True
    assert by_id["t-neg"].negatively_rated is True
    # The healthy, high-confidence, positively-rated trace is excluded.
    assert "t-fine" not in by_id


def test_positive_rating_and_unmatched_feedback_do_not_select() -> None:
    traces = [_trace("t1", "alpha question", confidence_score=0.9)]
    feedback = [
        _feedback("t1", rating=5),  # positive -> not a negative signal
        _feedback("ghost", rating=1),  # no matching trace -> ignored
    ]
    assert select_eligible_outcomes(traces, feedback, confidence_threshold=0.5) == []


def test_confidence_threshold_boundary_is_exclusive() -> None:
    # A score exactly at the threshold is not "below" it.
    traces = [_trace("t1", "alpha", confidence_score=0.5)]
    assert select_eligible_outcomes(traces, confidence_threshold=0.5) == []


# ---------------------------------------------------------------------------
# Minimum gate (R11.6)
# ---------------------------------------------------------------------------


def test_below_minimum_surfaces_minimum_without_topics() -> None:
    outcomes = [_outcome("t1", "alpha", low_confidence=True)]
    result = _generate(outcomes, min_eligible=5)

    assert isinstance(result, KnowledgeGapMap)
    assert result.topics == []
    assert result.eligible_outcome_count == 1
    assert result.configured_minimum == 5
    # No topics implies no recommendations are fabricated.
    assert result.recommended_missing_topics == []
    assert result.frequently_requested_topics == []


# ---------------------------------------------------------------------------
# Bounded clustering + well-formed topics (R11.1, R11.2)
# ---------------------------------------------------------------------------


def test_clustering_is_bounded_by_max_topics() -> None:
    # Five distinct topic keywords, but capped at 2 topics.
    outcomes = [
        _outcome(f"t{i}", f"{kw} question", low_confidence=True)
        for i, kw in enumerate(_TOPIC_KEYWORDS)
    ]
    result = _generate(outcomes, max_topics=2)

    assert 1 <= len(result.topics) <= 2
    # Every eligible outcome is assigned to exactly one topic.
    assert sum(t.contributing_question_count for t in result.topics) == len(outcomes)
    for topic in result.topics:
        assert topic.contributing_question_count >= 1
        assert topic.coverage_quality in {"poor", "fair", "good"}


def test_same_topic_questions_cluster_together() -> None:
    outcomes = [
        _outcome("t1", "alpha one", low_confidence=True),
        _outcome("t2", "alpha two", low_confidence=True),
        _outcome("t3", "beta one", low_confidence=True),
    ]
    result = _generate(outcomes, max_topics=25)

    counts = sorted(t.contributing_question_count for t in result.topics)
    assert counts == [1, 2]  # two alpha questions grouped, one beta alone


# ---------------------------------------------------------------------------
# Coverage-quality derivation (R11.2)
# ---------------------------------------------------------------------------


def test_coverage_quality_good_fair_poor() -> None:
    thresholds = CoverageThresholds()

    good = _generate(
        [
            _outcome("g1", "alpha a", confidence_score=0.9, low_confidence=False),
            _outcome("g2", "alpha b", confidence_score=0.85),
        ],
        thresholds=thresholds,
    )
    assert good.topics[0].coverage_quality == "good"

    poor = _generate(
        [
            _outcome("p1", "beta a", confidence_score=0.1, low_confidence=True),
            _outcome("p2", "beta b", confidence_score=0.2, low_confidence=True),
        ],
        thresholds=thresholds,
    )
    assert poor.topics[0].coverage_quality == "poor"

    fair = _generate(
        [
            _outcome("f1", "gamma a", confidence_score=0.55),
            _outcome("f2", "gamma b", confidence_score=0.6),
        ],
        thresholds=thresholds,
    )
    assert fair.topics[0].coverage_quality == "fair"


def test_high_negative_ratio_forces_poor() -> None:
    # High confidence but a majority-negative cluster is still poor.
    result = _generate(
        [
            _outcome("n1", "delta a", confidence_score=0.95, negatively_rated=True),
            _outcome("n2", "delta b", confidence_score=0.95, negatively_rated=True),
        ]
    )
    assert result.topics[0].coverage_quality == "poor"


# ---------------------------------------------------------------------------
# Recommendation categories (R11.4)
# ---------------------------------------------------------------------------


def test_recommendation_categories_are_populated() -> None:
    outcomes = [
        _outcome(
            "t1",
            "alpha broken",
            confidence_score=0.1,
            low_confidence=True,
            negatively_rated=True,
            document_ids=("doc-1", "doc-2"),
        ),
        _outcome("t2", "alpha also broken", confidence_score=0.15, low_confidence=True),
        _outcome("t3", "beta ok", confidence_score=0.9),
    ]
    result = _generate(outcomes)

    assert result.recommended_missing_topics  # under-covered topics
    assert set(result.documents_needing_reingestion) == {"doc-1", "doc-2"}
    assert "alpha broken" in result.suggested_benchmark_cases  # negatively rated
    assert result.frequently_requested_topics  # ordered topic labels
    # Frequently-requested topics are ordered by contributing count (desc).
    counts = [t.contributing_question_count for t in result.topics]
    ordered_labels = [
        t.topic
        for t in sorted(result.topics, key=lambda x: x.contributing_question_count, reverse=True)
    ]
    assert result.frequently_requested_topics == ordered_labels
    assert counts  # sanity


# ---------------------------------------------------------------------------
# Generation failure (R11.5)
# ---------------------------------------------------------------------------


def test_embedding_failure_raises_generation_error() -> None:
    def _boom(_question: str) -> list[float]:
        raise RuntimeError("titan unavailable")

    outcomes = [_outcome("t1", "alpha", low_confidence=True)]
    with pytest.raises(KnowledgeGapGenerationError) as excinfo:
        generate_knowledge_gap_map(
            outcomes,
            embed_question=_boom,
            label_cluster=_label,
            max_topics=25,
            min_eligible_outcomes=1,
        )
    assert excinfo.value.code == "knowledge_gap_generation_failed"


def test_empty_label_raises_generation_error() -> None:
    outcomes = [_outcome("t1", "alpha", low_confidence=True)]
    with pytest.raises(KnowledgeGapGenerationError):
        generate_knowledge_gap_map(
            outcomes,
            embed_question=_embed,
            label_cluster=lambda _questions: "   ",
            max_topics=25,
            min_eligible_outcomes=1,
        )


def test_nonpositive_max_topics_raises_generation_error() -> None:
    outcomes = [_outcome("t1", "alpha", low_confidence=True)]
    with pytest.raises(KnowledgeGapGenerationError):
        _generate(outcomes, max_topics=0)
