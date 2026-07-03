"""Retrieval quality metrics for evaluation (R7.2, R7.9).

When a ``BenchmarkCase`` carries ``Relevance_Labels`` the Evaluation_Service
computes recall@k, precision@k, and mean reciprocal rank (MRR@k) at the
configured retrieval depth against those labels rather than against the
expected answer alone (R7.2). When a case does not carry relevance labels no
retrieval metrics are computed and this module yields ``None`` (R7.9).

The functions here are pure: callers pass the ranked retrieved hits, the
relevance labels, and the depth ``k``. Ranking is derived from each hit's
retrieval ``score`` (descending, stable on ties), which mirrors the order in
which the retriever surfaced the hits.
"""

from __future__ import annotations

from collections.abc import Sequence

from rag_system.models import QueryTraceHit, RelevanceLabels, RetrievalMetrics


def relevant_identifiers(labels: RelevanceLabels) -> set[str]:
    """Collapse relevance labels into the set of relevant identifiers.

    A ground-truth item is relevant when it appears as a relevant chunk id, a
    relevant document id, or a human judgment with a positive score. Chunk and
    document identifiers share the same space for matching purposes so that a
    retrieved hit counts as relevant if either of its ids is labelled relevant.
    """

    relevant: set[str] = set()
    relevant.update(labels.relevant_chunk_ids)
    relevant.update(labels.relevant_document_ids)
    relevant.update(
        identifier
        for identifier, judgment in labels.human_judgments.items()
        if judgment > 0.0
    )
    return relevant


def _rank_hits(hits: Sequence[QueryTraceHit]) -> list[QueryTraceHit]:
    """Order hits by descending retrieval score, stable on ties."""

    return sorted(hits, key=lambda hit: hit.score, reverse=True)


def _hit_relevant_ids(hit: QueryTraceHit, relevant: set[str]) -> set[str]:
    """Relevant identifiers matched by a single hit (chunk or document id)."""

    return {hit.chunk_id, hit.document_id} & relevant


def compute_retrieval_metrics(
    hits: Sequence[QueryTraceHit],
    labels: RelevanceLabels | None,
    depth: int,
) -> RetrievalMetrics | None:
    """Compute recall@k, precision@k, and MRR@k against relevance labels.

    Returns ``None`` when ``labels`` is absent (R7.9). When labels are present
    the three metrics are each within ``[0.0, 1.0]``:

    - ``recall@k``: distinct relevant ground-truth ids matched within the top-k
      hits, divided by the total number of relevant ids.
    - ``precision@k``: relevant hits within the top-k, divided by the number of
      hits actually considered (``min(k, len(hits))``).
    - ``mrr@k``: reciprocal of the 1-based rank of the first relevant hit in the
      top-k, or ``0.0`` when none of the top-k hits are relevant.

    Empty label sets or empty retrieval both yield ``0.0`` for every metric.
    """

    if labels is None:
        return None

    k = max(depth, 0)
    relevant = relevant_identifiers(labels)
    top_hits = _rank_hits(hits)[:k]

    matched_ids: set[str] = set()
    relevant_hit_count = 0
    first_relevant_rank: int | None = None
    for position, hit in enumerate(top_hits, start=1):
        hit_matches = _hit_relevant_ids(hit, relevant)
        if hit_matches:
            relevant_hit_count += 1
            matched_ids.update(hit_matches)
            if first_relevant_rank is None:
                first_relevant_rank = position

    recall_at_k = len(matched_ids) / len(relevant) if relevant else 0.0
    precision_at_k = relevant_hit_count / len(top_hits) if top_hits else 0.0
    mrr_at_k = 1.0 / first_relevant_rank if first_relevant_rank is not None else 0.0

    return RetrievalMetrics(
        recall_at_k=recall_at_k,
        precision_at_k=precision_at_k,
        mrr_at_k=mrr_at_k,
        depth=k,
    )
