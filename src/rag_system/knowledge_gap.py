"""Knowledge gap map service (R11).

This module implements the pure logic behind the operator-only knowledge-gap
generation endpoint: it clusters **low-quality query outcomes** into topics and
produces *recommendations* for improving the Corpus. Consistent with the design
principle "recommendations, never silent automation" (design R11), nothing here
mutates configuration or corpus — it only reports.

Pipeline (see design R11):

1. **Eligibility (R11.1).** Scan the enriched
   :class:`~rag_system.models.QueryTraceRecord`s (task 1.7) joined with feedback
   for *eligible* outcomes: low-confidence (``confidence_score`` below the
   configured threshold), unanswered (an ``abstention_reason_code`` is set), or
   negatively rated (a joined :class:`~rag_system.models.FeedbackReviewRecord`
   with a negative rating). :func:`select_eligible_outcomes` does the join.
2. **Minimum gate (R11.6).** Generation runs only when the number of eligible
   outcomes meets ``knowledge_gap_min_eligible_outcomes``; below it the map is
   returned with **no topics** but carries ``eligible_outcome_count`` and
   ``configured_minimum`` so the frontend can show the insufficient-outcomes
   notice — the service surfaces the minimum rather than generating.
3. **Clustering (R11.1, R11.2).** Each eligible outcome's question is embedded
   (Titan embeddings, injected as ``embed_question`` so tests stub it) and the
   embeddings are grouped by cosine similarity into **no more than**
   ``knowledge_gap_max_topics`` topics. Every eligible outcome lands in exactly
   one topic; only eligible outcomes are clustered.
4. **Coverage quality (R11.2).** Each topic gets a ``poor`` / ``fair`` / ``good``
   level derived from *aggregate* cluster signals — average answer confidence and
   the cluster's negative-feedback ratio — via configurable thresholds, plus a
   non-negative contributing-question count.
5. **Topic labels.** A human-readable label per cluster is produced by LLM
   summarization over the cluster's representative questions (injected as
   ``label_cluster`` so tests stub the model output).
6. **Recommendations (R11.4).** The map recommends missing topics/source types,
   documents needing re-ingestion, suggested golden benchmark cases, and
   frequently requested topics.

On any failure during generation the service raises
:class:`KnowledgeGapGenerationError` (code ``knowledge_gap_generation_failed``),
which the endpoint (task 18.4) maps to a structured error (R11.5).

Like :mod:`rag_system.feedback` and :mod:`rag_system.corpus`, this module is
pure and deterministic given its inputs and injected callables, so it is
trivially unit- and property-testable without storage, HTTP, or live models.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .feedback import is_negative_rating
from .models import (
    FeedbackReviewRecord,
    KnowledgeGapMap,
    KnowledgeGapTopic,
    QueryTraceRecord,
)

__all__ = [
    "EligibleOutcome",
    "CoverageThresholds",
    "KnowledgeGapError",
    "KnowledgeGapGenerationError",
    "EmbedQuestion",
    "LabelCluster",
    "select_eligible_outcomes",
    "generate_knowledge_gap_map",
]

#: Embeds a single question into a dense vector (Titan embeddings in production,
#: stubbed in tests for determinism).
EmbedQuestion = Callable[[str], Sequence[float]]

#: Summarizes a cluster's representative questions into a human-readable topic
#: label (``gemini-3.5-flash`` in production, stubbed in tests).
LabelCluster = Callable[[Sequence[str]], str]

#: Number of representative questions handed to the labeler / used as suggested
#: benchmark cases per cluster.
_REPRESENTATIVE_LIMIT = 5

#: Cosine-similarity threshold above which an outcome joins an existing cluster
#: rather than seeding a new one. Kept internal; clustering is additionally
#: bounded by ``knowledge_gap_max_topics``.
_SIMILARITY_THRESHOLD = 0.75


class KnowledgeGapError(Exception):
    """Base class for knowledge-gap errors carrying a stable ``code``."""

    code = "knowledge_gap_error"


class KnowledgeGapGenerationError(KnowledgeGapError):
    """Raised when the Knowledge_Gap_Map cannot be generated (R11.5)."""

    code = "knowledge_gap_generation_failed"


@dataclass(frozen=True)
class CoverageThresholds:
    """Configurable thresholds mapping aggregate cluster signals to a
    coverage-quality level (R11.2).

    * ``good`` — average confidence is high **and** the negative-feedback ratio
      is low.
    * ``poor`` — average confidence is low **or** the negative-feedback ratio is
      high.
    * ``fair`` — anything else.
    """

    #: Average confidence at/above which a cluster may be ``good``.
    good_min_confidence: float = 0.7
    #: Average confidence below which a cluster is ``poor``.
    poor_max_confidence: float = 0.4
    #: Negative-feedback ratio at/below which a cluster may be ``good``.
    good_max_negative_ratio: float = 0.2
    #: Negative-feedback ratio at/above which a cluster is ``poor``.
    poor_min_negative_ratio: float = 0.5


@dataclass(frozen=True)
class EligibleOutcome:
    """One low-quality query outcome eligible for knowledge-gap clustering.

    An outcome is eligible when at least one of ``low_confidence``,
    ``abstained``, or ``negatively_rated`` holds (R11.1).
    """

    trace_id: str
    question: str
    confidence_score: float | None = None
    low_confidence: bool = False
    abstained: bool = False
    negatively_rated: bool = False
    document_ids: tuple[str, ...] = ()
    expected_answer: str | None = None

    @property
    def is_eligible(self) -> bool:
        return self.low_confidence or self.abstained or self.negatively_rated


def select_eligible_outcomes(
    traces: Iterable[QueryTraceRecord],
    feedback_items: Iterable[FeedbackReviewRecord] = (),
    *,
    confidence_threshold: float,
) -> list[EligibleOutcome]:
    """Join traces with feedback and return the eligible outcomes (R11.1).

    An outcome is eligible when the trace is low-confidence (``confidence_score``
    strictly below ``confidence_threshold``), unanswered (an
    ``abstention_reason_code`` is set), or negatively rated (a joined
    :class:`FeedbackReviewRecord` with a negative rating references the trace).

    Args:
        traces: The enriched query trace records to scan.
        feedback_items: Feedback records used to flag negatively rated traces;
            items whose ``trace_id`` has no matching trace are ignored (there is
            no outcome to attribute the negative rating to).
        confidence_threshold: The configured confidence threshold below which an
            outcome counts as low-confidence.

    Returns:
        The eligible outcomes, in trace input order, each carrying the flags that
        made it eligible plus context used for recommendations.
    """
    negative_trace_ids: set[str] = {
        item.trace_id
        for item in feedback_items
        if is_negative_rating(item.rating)
    }

    outcomes: list[EligibleOutcome] = []
    for trace in traces:
        score = trace.confidence_score
        low_confidence = score is not None and score < confidence_threshold
        abstained = trace.abstention_reason_code is not None
        negatively_rated = trace.trace_id in negative_trace_ids

        if not (low_confidence or abstained or negatively_rated):
            continue

        outcomes.append(
            EligibleOutcome(
                trace_id=trace.trace_id,
                question=trace.question,
                confidence_score=score,
                low_confidence=low_confidence,
                abstained=abstained,
                negatively_rated=negatively_rated,
                document_ids=tuple(trace.document_ids or ()),
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# Embedding-based clustering
# ---------------------------------------------------------------------------


@dataclass
class _Cluster:
    """Mutable accumulator for one topic during clustering."""

    #: Running sum of member embeddings; the centroid is this divided by size.
    _sum: list[float]
    members: list[EligibleOutcome] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    def centroid(self) -> list[float]:
        if not self.members:
            return list(self._sum)
        return [component / self.size for component in self._sum]

    def add(self, outcome: EligibleOutcome, embedding: Sequence[float]) -> None:
        if not self._sum:
            self._sum = [float(v) for v in embedding]
        else:
            for i, v in enumerate(embedding):
                self._sum[i] += float(v)
        self.members.append(outcome)


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors; ``0.0`` for a zero vector."""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _cluster_outcomes(
    outcomes: Sequence[EligibleOutcome],
    embeddings: Sequence[Sequence[float]],
    max_topics: int,
) -> list[_Cluster]:
    """Group outcomes into at most ``max_topics`` clusters (R11.1).

    A deterministic, dependency-free greedy pass: each outcome joins the most
    similar existing cluster when their cosine similarity clears
    ``_SIMILARITY_THRESHOLD``; otherwise it seeds a new cluster. Once
    ``max_topics`` clusters exist, every remaining outcome is folded into its
    nearest cluster so the bound holds and no eligible outcome is dropped.
    """
    clusters: list[_Cluster] = []
    for outcome, embedding in zip(outcomes, embeddings, strict=True):
        best_index = -1
        best_similarity = -1.0
        for index, cluster in enumerate(clusters):
            similarity = _cosine_similarity(embedding, cluster.centroid())
            if similarity > best_similarity:
                best_similarity = similarity
                best_index = index

        at_capacity = len(clusters) >= max_topics
        if best_index >= 0 and (best_similarity >= _SIMILARITY_THRESHOLD or at_capacity):
            clusters[best_index].add(outcome, embedding)
        else:
            new_cluster = _Cluster(_sum=[])
            new_cluster.add(outcome, embedding)
            clusters.append(new_cluster)

    return clusters


# ---------------------------------------------------------------------------
# Coverage quality + topic assembly
# ---------------------------------------------------------------------------


def _average_confidence(members: Sequence[EligibleOutcome]) -> float:
    """Average confidence over members; absent scores count as ``0.0``.

    Unanswered (abstained) outcomes frequently carry no ``confidence_score``;
    treating a missing score as ``0.0`` keeps a cluster of abstentions on the
    low-confidence (``poor``) end rather than silently omitting it.
    """
    if not members:
        return 0.0
    total = sum(m.confidence_score if m.confidence_score is not None else 0.0 for m in members)
    return total / len(members)


def _negative_ratio(members: Sequence[EligibleOutcome]) -> float:
    """Fraction of members that were negatively rated."""
    if not members:
        return 0.0
    negatives = sum(1 for m in members if m.negatively_rated)
    return negatives / len(members)


def _coverage_quality(
    members: Sequence[EligibleOutcome],
    thresholds: CoverageThresholds,
) -> str:
    """Derive ``poor`` / ``fair`` / ``good`` from aggregate cluster signals."""
    avg_confidence = _average_confidence(members)
    negative_ratio = _negative_ratio(members)

    if (
        avg_confidence < thresholds.poor_max_confidence
        or negative_ratio >= thresholds.poor_min_negative_ratio
    ):
        return "poor"
    if (
        avg_confidence >= thresholds.good_min_confidence
        and negative_ratio <= thresholds.good_max_negative_ratio
    ):
        return "good"
    return "fair"


def _representative_questions(members: Sequence[EligibleOutcome]) -> list[str]:
    """Up to ``_REPRESENTATIVE_LIMIT`` distinct questions representing a cluster."""
    seen: set[str] = set()
    questions: list[str] = []
    for member in members:
        # Skip blank questions so labels and suggested cases are never empty.
        if not member.question.strip():
            continue
        if member.question in seen:
            continue
        seen.add(member.question)
        questions.append(member.question)
        if len(questions) >= _REPRESENTATIVE_LIMIT:
            break
    return questions


@dataclass(frozen=True)
class _Topic:
    """A fully-assembled topic plus the source signals used for recommendations."""

    label: str
    coverage_quality: str
    members: tuple[EligibleOutcome, ...]

    @property
    def count(self) -> int:
        return len(self.members)


def _build_recommendations(topics: Sequence[_Topic]) -> dict[str, list[str]]:
    """Derive the four recommendation categories from the assembled topics (R11.4)."""
    # Missing topics / source types to improve coverage: the under-covered
    # topics (poor first, then fair). Every clustered topic is a knowledge gap,
    # so fall back to all topic labels when none are explicitly poor/fair.
    under_covered = [t.label for t in topics if t.coverage_quality == "poor"]
    under_covered += [t.label for t in topics if t.coverage_quality == "fair"]
    if not under_covered:
        under_covered = [t.label for t in topics]

    # Documents needing re-ingestion: documents implicated in poor topics.
    documents: list[str] = []
    seen_docs: set[str] = set()
    for topic in topics:
        if topic.coverage_quality != "poor":
            continue
        for member in topic.members:
            for doc_id in member.document_ids:
                if doc_id not in seen_docs:
                    seen_docs.add(doc_id)
                    documents.append(doc_id)

    # Suggested golden benchmark cases: representative questions drawn from the
    # negatively rated outcomes (the clearest "should have answered" signal),
    # falling back to representative questions of poor topics.
    benchmark_cases: list[str] = []
    seen_cases: set[str] = set()

    def _add_case(question: str) -> None:
        # Never surface a blank suggested benchmark case.
        if not question.strip():
            return
        if question not in seen_cases:
            seen_cases.add(question)
            benchmark_cases.append(question)

    for topic in topics:
        for member in topic.members:
            if member.negatively_rated:
                _add_case(member.question)
    if not benchmark_cases:
        for topic in topics:
            if topic.coverage_quality == "poor":
                for question in _representative_questions(topic.members):
                    _add_case(question)

    # Frequently requested topics: topic labels ordered by contributing count.
    frequently_requested = [
        topic.label
        for topic in sorted(topics, key=lambda t: t.count, reverse=True)
    ]

    return {
        "recommended_missing_topics": under_covered,
        "documents_needing_reingestion": documents,
        "suggested_benchmark_cases": benchmark_cases,
        "frequently_requested_topics": frequently_requested,
    }


def generate_knowledge_gap_map(
    outcomes: Sequence[EligibleOutcome],
    *,
    embed_question: EmbedQuestion,
    label_cluster: LabelCluster,
    max_topics: int,
    min_eligible_outcomes: int,
    thresholds: CoverageThresholds | None = None,
) -> KnowledgeGapMap:
    """Generate a :class:`KnowledgeGapMap` from eligible outcomes (R11.1–R11.6).

    Args:
        outcomes: The eligible outcomes (see :func:`select_eligible_outcomes`).
        embed_question: Embeds a question into a dense vector (Titan in
            production, stubbed in tests).
        label_cluster: Summarizes a cluster's representative questions into a
            human-readable topic label (``gemini-3.5-flash`` in production).
        max_topics: Maximum number of topics (``knowledge_gap_max_topics``); the
            clustering never exceeds this (R11.1).
        min_eligible_outcomes: Minimum eligible outcomes required to cluster
            (``knowledge_gap_min_eligible_outcomes``); below it no topics are
            produced and the map surfaces the minimum for the notice (R11.6).
        thresholds: Coverage-quality thresholds; defaults to
            :class:`CoverageThresholds`.

    Returns:
        A :class:`KnowledgeGapMap`. When there are too few eligible outcomes it
        carries no topics (empty recommendations) but records
        ``eligible_outcome_count`` and ``configured_minimum``.

    Raises:
        KnowledgeGapGenerationError: Generation failed (R11.5) — for example the
            embedding or labeling step raised.
    """
    thresholds = thresholds or CoverageThresholds()
    eligible_count = len(outcomes)

    # R11.6: below the configured minimum, surface the minimum rather than
    # generating any topics.
    if eligible_count < min_eligible_outcomes:
        return KnowledgeGapMap(
            topics=[],
            eligible_outcome_count=eligible_count,
            configured_minimum=min_eligible_outcomes,
        )

    if max_topics < 1:
        raise KnowledgeGapGenerationError(
            f"knowledge_gap_max_topics must be positive, got {max_topics!r}"
        )

    try:
        embeddings = [list(embed_question(outcome.question)) for outcome in outcomes]
        clusters = _cluster_outcomes(outcomes, embeddings, max_topics)

        topics: list[_Topic] = []
        for cluster in clusters:
            if cluster.size == 0:  # defensive; clusters always have members
                continue
            representatives = _representative_questions(cluster.members)
            label = label_cluster(representatives)
            if not isinstance(label, str) or not label.strip():
                raise KnowledgeGapGenerationError(
                    "cluster labeler returned an empty topic label"
                )
            topics.append(
                _Topic(
                    label=label.strip(),
                    coverage_quality=_coverage_quality(cluster.members, thresholds),
                    members=tuple(cluster.members),
                )
            )
    except KnowledgeGapGenerationError:
        raise
    except Exception as exc:  # embedding/labeling/clustering failure (R11.5)
        raise KnowledgeGapGenerationError(
            f"knowledge gap map generation failed: {exc}"
        ) from exc

    recommendations = _build_recommendations(topics)

    return KnowledgeGapMap(
        topics=[
            KnowledgeGapTopic(
                topic=topic.label,
                coverage_quality=topic.coverage_quality,
                contributing_question_count=topic.count,
            )
            for topic in topics
        ],
        recommended_missing_topics=recommendations["recommended_missing_topics"],
        documents_needing_reingestion=recommendations["documents_needing_reingestion"],
        suggested_benchmark_cases=recommendations["suggested_benchmark_cases"],
        frequently_requested_topics=recommendations["frequently_requested_topics"],
        eligible_outcome_count=eligible_count,
        configured_minimum=min_eligible_outcomes,
    )
