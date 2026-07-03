"""Property-based test for bounded clustering and topic shape (R11.1, R11.2).

# Feature: rag-trust-and-observability, Property 37: Knowledge gap clustering is bounded and topics are well-formed

**Validates: Requirements 11.1, 11.2**

For any set of eligible query outcomes (low-confidence, unanswered/abstained, or
negatively rated), the generated ``KnowledgeGapMap`` satisfies:

1. The number of topics produced is bounded by ``knowledge_gap_max_topics`` (default 25).
2. Each topic has a non-empty label.
3. Each topic has a ``coverage_quality`` value in {``poor``, ``fair``, ``good``}.
4. Each topic has a non-negative contributing-question count.
5. Generation is gated on ``knowledge_gap_min_eligible_outcomes`` — when the number
   of eligible outcomes is below the configured minimum, no topics are generated and
   the map surfaces the minimum rather than generating.
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

# Deterministic embedding: hash the question into a fixed-dimension vector so
# that clustering is reproducible without external models.
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

_eligible_outcomes = st.builds(
    EligibleOutcome,
    trace_id=st.uuids().map(str),
    question=st.text(min_size=1, max_size=80, alphabet=st.characters(categories=("L", "N", "Zs"))),
    confidence_score=_confidence_scores,
    low_confidence=st.booleans(),
    abstained=st.booleans(),
    negatively_rated=st.booleans(),
).filter(lambda o: o.is_eligible)

# At least 1 outcome, up to 50 to exercise merging when exceeding max_topics.
_outcome_lists = st.lists(_eligible_outcomes, min_size=1, max_size=50)

# max_topics between 1 and 30 (design default is 25).
_max_topics = st.integers(min_value=1, max_value=30)

# min_eligible_outcomes: between 1 and 60 to exercise both the gate-open and
# gate-closed paths in a single property.
_min_eligible = st.integers(min_value=1, max_value=60)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    outcomes=_outcome_lists,
    max_topics=_max_topics,
    min_eligible=_min_eligible,
)
def test_knowledge_gap_clustering_bounded_and_topics_well_formed(
    outcomes: list[EligibleOutcome],
    max_topics: int,
    min_eligible: int,
) -> None:
    """**Validates: Requirements 11.1, 11.2**

    Property 37: Knowledge gap clustering is bounded and topics are well-formed.
    """
    result = generate_knowledge_gap_map(
        outcomes,
        embed_question=_embed,
        label_cluster=_label,
        max_topics=max_topics,
        min_eligible_outcomes=min_eligible,
    )

    assert isinstance(result, KnowledgeGapMap)

    eligible_count = len(outcomes)

    # --- Minimum gate (R11.6 / property point 5) ---
    if eligible_count < min_eligible:
        # Below the configured minimum: no topics produced, minimum surfaced.
        assert result.topics == []
        assert result.eligible_outcome_count == eligible_count
        assert result.configured_minimum == min_eligible
        return  # No further assertions needed for the gated path.

    # --- Bounded clustering (R11.1 / property point 1) ---
    assert len(result.topics) <= max_topics
    # At least one topic should be produced when outcomes meet the minimum.
    assert len(result.topics) >= 1

    for topic in result.topics:
        # --- Non-empty label (property point 2) ---
        assert isinstance(topic.topic, str)
        assert len(topic.topic.strip()) > 0

        # --- Coverage quality in allowed set (R11.2 / property point 3) ---
        assert topic.coverage_quality in {"poor", "fair", "good"}

        # --- Non-negative contributing-question count (R11.2 / property point 4) ---
        assert topic.contributing_question_count >= 0

    # --- Total contributing counts sum to exactly the number of eligible outcomes ---
    # (every eligible outcome is assigned to exactly one topic).
    total_count = sum(t.contributing_question_count for t in result.topics)
    assert total_count == eligible_count

    # --- eligible_outcome_count and configured_minimum are recorded ---
    assert result.eligible_outcome_count == eligible_count
    assert result.configured_minimum == min_eligible
