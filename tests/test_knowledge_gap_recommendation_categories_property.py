# Feature: rag-trust-and-observability, Property 38: Knowledge gap map includes all recommendation categories
"""Property-based test for recommendation categories (R11.3, R11.4).

# Feature: rag-trust-and-observability, Property 38: Knowledge gap map includes all recommendation categories

**Validates: Requirements 11.3, 11.4**

For any successfully generated ``KnowledgeGapMap`` (i.e. eligible outcomes meet
the configured minimum and generation succeeds), the map includes recommendations
in all four categories:

1. ``recommended_missing_topics`` — missing topics/source types to improve coverage.
2. ``documents_needing_reingestion`` — documents that need re-ingestion.
3. ``suggested_benchmark_cases`` — suggested golden benchmark cases.
4. ``frequently_requested_topics`` — frequently requested topics ordered by count.

Each category field is present (even if an empty list), and when populated,
contains well-formed items (non-empty strings).

Additionally, each rendered topic displays its ``coverage_quality`` level and a
non-negative ``contributing_question_count``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.knowledge_gap import (
    EligibleOutcome,
    generate_knowledge_gap_map,
)
from rag_system.models import KnowledgeGapMap

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_EMBED_DIM = 8


def _embed(question: str) -> list[float]:
    """Deterministic embedding: distribute question hash across dimensions."""
    h = hash(question) & 0xFFFFFFFF
    return [float((h >> (i * 4)) & 0xF) / 15.0 for i in range(_EMBED_DIM)]


def _label(questions: list[str]) -> str:
    """Deterministic labeler: join first few words of the first question."""
    if not questions:
        return "unlabeled"
    return " ".join(questions[0].split()[:4]) or "topic"


_confidence_scores = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))

# Document IDs: small overlapping space so recommendations are exercised.
_doc_ids = st.sampled_from(["doc-a", "doc-b", "doc-c", "doc-d", "doc-e", ""])

_eligible_outcomes = st.builds(
    EligibleOutcome,
    trace_id=st.uuids().map(str),
    question=st.text(min_size=1, max_size=80, alphabet=st.characters(categories=("L", "N", "Zs"))),
    confidence_score=_confidence_scores,
    low_confidence=st.booleans(),
    abstained=st.booleans(),
    negatively_rated=st.booleans(),
    document_ids=st.tuples(_doc_ids, _doc_ids).map(lambda t: tuple(d for d in t if d)),
).filter(lambda o: o.is_eligible)

# Enough outcomes to exceed any reasonable min_eligible so we always generate.
_outcome_lists = st.lists(_eligible_outcomes, min_size=1, max_size=50)

# max_topics between 1 and 30 (design default is 25).
_max_topics = st.integers(min_value=1, max_value=30)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    outcomes=_outcome_lists,
    max_topics=_max_topics,
)
def test_knowledge_gap_map_includes_all_recommendation_categories(
    outcomes: list[EligibleOutcome],
    max_topics: int,
) -> None:
    """**Validates: Requirements 11.3, 11.4**

    Property 38: Knowledge gap map includes all recommendation categories.

    When the knowledge gap map is generated successfully (eligible outcomes meet
    the minimum), it includes recommendations in all four categories. Each
    category is present (even if empty list), and recommendations are well-formed
    (non-empty strings).
    """
    # Use min_eligible_outcomes=1 so we always generate (outcomes has >= 1 item).
    result = generate_knowledge_gap_map(
        outcomes,
        embed_question=_embed,
        label_cluster=_label,
        max_topics=max_topics,
        min_eligible_outcomes=1,
    )

    assert isinstance(result, KnowledgeGapMap)

    # --- All four recommendation category fields are present (R11.4) ---
    assert isinstance(result.recommended_missing_topics, list)
    assert isinstance(result.documents_needing_reingestion, list)
    assert isinstance(result.suggested_benchmark_cases, list)
    assert isinstance(result.frequently_requested_topics, list)

    # --- When populated, each recommendation item is a non-empty string ---
    for item in result.recommended_missing_topics:
        assert isinstance(item, str)
        assert len(item.strip()) > 0, "recommended_missing_topics contains empty string"

    for item in result.documents_needing_reingestion:
        assert isinstance(item, str)
        assert len(item.strip()) > 0, "documents_needing_reingestion contains empty string"

    for item in result.suggested_benchmark_cases:
        assert isinstance(item, str)
        assert len(item.strip()) > 0, "suggested_benchmark_cases contains empty string"

    for item in result.frequently_requested_topics:
        assert isinstance(item, str)
        assert len(item.strip()) > 0, "frequently_requested_topics contains empty string"

    # --- Each topic displays coverage_quality and contributing_question_count (R11.3) ---
    for topic in result.topics:
        assert topic.coverage_quality in {"poor", "fair", "good"}
        assert topic.contributing_question_count >= 0

    # --- frequently_requested_topics are ordered by contributing count (descending) ---
    # The service orders them by count; verify monotonicity via the topic model.
    if len(result.topics) > 1:
        topic_by_label = {t.topic: t.contributing_question_count for t in result.topics}
        counts_in_freq_order = [
            topic_by_label[label]
            for label in result.frequently_requested_topics
            if label in topic_by_label
        ]
        for i in range(len(counts_in_freq_order) - 1):
            assert counts_in_freq_order[i] >= counts_in_freq_order[i + 1], (
                "frequently_requested_topics not ordered by count descending"
            )

    # --- recommended_missing_topics is non-empty when topics are generated ---
    # Per the implementation, under-covered topics (poor/fair) always populate
    # this list, falling back to all topic labels; so when topics exist, at
    # least one missing topic recommendation is produced.
    if result.topics:
        assert len(result.recommended_missing_topics) >= 1

    # --- frequently_requested_topics matches topic count ---
    # Every topic label appears once in the frequently_requested list.
    assert len(result.frequently_requested_topics) == len(result.topics)
