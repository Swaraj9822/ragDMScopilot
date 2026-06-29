"""Numeric confidence scoring for generated outputs.

Every user-facing answer the system produces — RAG document answers, the
database (text-to-SQL) copilot, and the unified router that blends both —
carries a numeric ``confidence_score`` in ``[0, 1]`` alongside the existing
categorical signals (``evidence_status`` and the ``confidence`` label).

The score is derived from explainable signals already available at answer
time, so it is deterministic and unit-testable and needs no extra LLM round
trip. When the model exposes token-level log-probabilities, they are folded
in through ``avg_logprob`` — but the score degrades gracefully to the
grounding signals when that information is absent.

Design notes
------------
* The score is a transparent weighted average of independent sub-signals.
  Signals that are unavailable (``None``) are dropped and the remaining
  weights are renormalised, so the score is always well defined.
* We never emit ``0.0`` or ``1.0``: a generated answer is never absolutely
  certain, and a returned answer is never worthless. Scores are clamped to
  ``[MIN_SCORE, MAX_SCORE]``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

# Score bounds — never claim absolute certainty nor absolute worthlessness.
MIN_SCORE = 0.05
MAX_SCORE = 0.99

# Base score implied by the qualitative evidence status of an answer.
_EVIDENCE_BASE: dict[str, float] = {
    "grounded": 0.85,
    "partially_grounded": 0.6,
    "no_rows": 0.3,
    "insufficient_evidence": 0.15,
}
_DEFAULT_EVIDENCE_BASE = 0.4

# Numeric reading of the model's own self-reported confidence label.
_LABEL_SCORE: dict[str, float] = {"high": 0.9, "medium": 0.6, "low": 0.3}

# Hard ceiling applied when the generator flagged the evidence as insufficient.
_INSUFFICIENT_CEILING = 0.35


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _weighted_average(components: Iterable[tuple[float | None, float]]) -> float | None:
    """Average ``(score, weight)`` pairs, ignoring ``None`` scores.

    Returns ``None`` only when no component carries usable information.
    """
    present = [(score, weight) for score, weight in components if score is not None and weight > 0]
    if not present:
        return None
    total_weight = sum(weight for _, weight in present)
    return sum(score * weight for score, weight in present) / total_weight


def _evidence_component(evidence_status: str | None) -> float:
    return _EVIDENCE_BASE.get((evidence_status or "").strip().lower(), _DEFAULT_EVIDENCE_BASE)


def _label_component(label: str | None) -> float | None:
    if not label:
        return None
    return _LABEL_SCORE.get(str(label).strip().lower())


def _retrieval_component(
    retrieval_scores: Sequence[float] | None,
    citation_count: int,
    hit_count: int,
) -> float | None:
    """Grounding quality: how strong retrieval was *and* how much was cited."""
    if not hit_count:
        return None
    normalised = [_clamp(float(score)) for score in (retrieval_scores or [])]
    mean_score = sum(normalised) / len(normalised) if normalised else 0.0
    citation_ratio = _clamp(citation_count / hit_count)
    return 0.5 * mean_score + 0.5 * citation_ratio


def _logprob_component(avg_logprob: float | None) -> float | None:
    """Convert an average per-token natural-log probability into ``[0, 1]``."""
    if avg_logprob is None:
        return None
    return _clamp(math.exp(avg_logprob))


def rag_confidence_score(
    *,
    evidence_status: str,
    model_confidence: str | None = None,
    retrieval_scores: Sequence[float] | None = None,
    citation_count: int = 0,
    hit_count: int = 0,
    has_insufficient_reason: bool = False,
    avg_logprob: float | None = None,
) -> float:
    """Confidence in a RAG document answer.

    Combines the qualitative evidence status, the model's self-reported
    confidence label, retrieval/citation grounding, and (when available) the
    model's token log-probabilities.
    """
    components = [
        (_evidence_component(evidence_status), 0.40),
        (_label_component(model_confidence), 0.20),
        (_retrieval_component(retrieval_scores, citation_count, hit_count), 0.25),
        (_logprob_component(avg_logprob), 0.15),
    ]
    score = _weighted_average(components)
    if score is None:
        score = _DEFAULT_EVIDENCE_BASE
    if has_insufficient_reason:
        score = min(score, _INSUFFICIENT_CEILING)
    return round(_clamp(score, MIN_SCORE, MAX_SCORE), 3)


def database_confidence_score(
    *,
    evidence_status: str,
    row_count: int = 0,
    sql_validated: bool = True,
    avg_logprob: float | None = None,
) -> float:
    """Confidence in a database (text-to-SQL) copilot answer.

    A validated SELECT that returned rows is a strong signal the question was
    answerable; a query that returned no rows is inherently weaker. SQL that
    failed validation collapses to the floor score.
    """
    if not sql_validated:
        return MIN_SCORE

    evidence = _evidence_component(evidence_status)
    rows_component: float | None = None
    if (evidence_status or "").strip().lower() == "grounded":
        rows_component = 0.9 if row_count else 0.6

    components = [
        (evidence, 0.60),
        (rows_component, 0.25),
        (_logprob_component(avg_logprob), 0.15),
    ]
    score = _weighted_average(components)
    if score is None:
        score = evidence
    return round(_clamp(score, MIN_SCORE, MAX_SCORE), 3)


def combine_confidence_scores(scores: Iterable[float | None]) -> float | None:
    """Blend per-branch scores for a hybrid answer.

    A hybrid answer is only as trustworthy as its weaker grounded branch, so
    we blend the mean with the minimum to stay conservative. Returns ``None``
    when no branch produced a score.
    """
    present = [score for score in scores if score is not None]
    if not present:
        return None
    mean = sum(present) / len(present)
    combined = 0.6 * mean + 0.4 * min(present)
    return round(_clamp(combined, MIN_SCORE, MAX_SCORE), 3)


def confidence_band(score: float | None) -> str | None:
    """Map a numeric score to a coarse ``high``/``medium``/``low`` band."""
    if score is None:
        return None
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"
