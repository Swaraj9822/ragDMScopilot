"""Evidence-based abstention (R3).

This module centralizes the six abstention triggers behind a single pure
function, :func:`evaluate_abstention`. Given the signals produced along the
answer path it returns *at most one* :class:`AbstentionResponse` carrying
exactly one :class:`ReasonCode` and a bounded ``missing_information``
description (1..1000 chars, R3.8), and no answer / claims / evidence content
(R3.7).

Determinism and precedence
--------------------------
The six triggers are evaluated in a **fixed, deterministic precedence** so that
exactly one reason code is ever chosen. The order mirrors the answer-path
control flow in the design: retrieval gates are evaluated *pre-generation*, and
the claim / confidence gates *post-generation*:

1. ``no_evidence`` — retrieval ran and returned nothing (R3.2).
2. ``retrieval_below_threshold`` — retrieval returned hits but every score is
   below the configured threshold (R3.6).
3. ``low_confidence`` — the confidence score is below the route minimum (R3.1).
4. ``unsupported_claims`` — one or more *material* claims are ``unsupported``
   (R3.3).
5. ``conflicting_evidence`` — a claim carries contradictory evidence (R3.4).
6. ``sql_no_rows`` — the SQL route executed and returned no rows (R3.5).

Route-appropriateness
----------------------
The retrieval gates only apply to routes that actually perform passage
retrieval. Callers signal "retrieval was not performed" by passing
``retrieval_scores=None`` (the default); an *empty* sequence (``[]``) means
retrieval ran and returned zero hits, which fires ``no_evidence``. Likewise the
SQL gate only fires for the ``database`` route when a row count of ``0`` is
supplied.

Materiality (R3.3)
------------------
Every decomposed factual :class:`Claim` is *material by default* — decomposition
(R1.1) already extracts only factual statements — so ``unsupported_claims`` fires
when **any** claim has ``evidence_status == unsupported``. The materiality
predicate is injectable via ``is_material`` so a future settings hook can exclude
classes of claims without changing this function.

Conflicting evidence (R3.4)
---------------------------
A claim has conflicting evidence when it is associated with at least one
``entails`` evidence item **and** at least one ``does_not_entail`` evidence item
drawn from *different* ``document_id``s. Callers that already computed this flag
during verification (R1.3) may pass the set of flagged claim ids via
``conflicting_claim_ids``; otherwise the condition is derived directly from the
claims' evidence items by :func:`has_conflicting_evidence`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence

from .models import (
    AbstentionResponse,
    Claim,
    EvidenceStatus,
    ReasonCode,
    VerificationResult,
)

#: The SQL / database route, the only route for which ``sql_no_rows`` fires.
DATABASE_ROUTE = "database"

#: Default missing-information descriptions per reason code (all within the
#: 1..1000 char bound required by R3.8). Callers may override any of these via
#: the ``missing_information`` argument to :func:`evaluate_abstention`.
DEFAULT_MISSING_INFORMATION: dict[ReasonCode, str] = {
    ReasonCode.low_confidence: (
        "The system was not confident enough in its answer for this question. "
        "There was not enough reliable supporting evidence to respond with "
        "confidence."
    ),
    ReasonCode.no_evidence: (
        "No supporting evidence was retrieved for this question, so the system "
        "cannot provide a grounded answer."
    ),
    ReasonCode.unsupported_claims: (
        "One or more parts of the answer could not be supported by the retrieved "
        "evidence, so the system is withholding the answer rather than presenting "
        "unsupported claims."
    ),
    ReasonCode.conflicting_evidence: (
        "The retrieved evidence contains conflicting information from different "
        "sources, so the system cannot determine a single well-supported answer."
    ),
    ReasonCode.sql_no_rows: (
        "The database query for this question returned no matching rows, so there "
        "is no data available to answer it."
    ),
    ReasonCode.retrieval_below_threshold: (
        "The retrieved passages were all below the relevance threshold, so none "
        "were strong enough to ground an answer."
    ),
}

#: Fallback used when a supplied / overridden description is empty; keeps the
#: response within the ``min_length=1`` bound of :class:`AbstentionResponse`.
_GENERIC_MISSING_INFORMATION = (
    "The system lacks sufficient supporting evidence to answer this question."
)

#: The maximum length of a missing-information description (R3.8).
_MAX_MISSING_INFORMATION = 1000


def default_is_material(claim: Claim) -> bool:  # noqa: ARG001 - injectable predicate
    """Default materiality predicate: every decomposed claim is material (R3.3)."""
    return True


def has_conflicting_evidence(claim: Claim) -> bool:
    """Return whether ``claim`` carries contradictory evidence (R3.4).

    A claim conflicts when it has at least one ``entails`` evidence item and at
    least one ``does_not_entail`` evidence item that originate from *different*
    documents (differing ``document_id``).
    """
    entails_docs: set[str | None] = set()
    refutes_docs: set[str | None] = set()
    for item in claim.evidence_items:
        if item.verification_result == VerificationResult.entails:
            entails_docs.add(item.document_id)
        elif item.verification_result == VerificationResult.does_not_entail:
            refutes_docs.add(item.document_id)

    if not entails_docs or not refutes_docs:
        return False
    # Contradiction requires the supporting and refuting items to come from
    # different sources.
    return any(
        entails_doc != refutes_doc
        for entails_doc in entails_docs
        for refutes_doc in refutes_docs
    )


def _resolve_missing_information(
    reason_code: ReasonCode,
    overrides: Mapping[ReasonCode, str] | None,
) -> str:
    """Pick, clamp, and sanitize the description for ``reason_code`` (R3.8)."""
    description: str | None = None
    if overrides is not None:
        description = overrides.get(reason_code)
    if description is None:
        description = DEFAULT_MISSING_INFORMATION.get(reason_code)
    if not description or not description.strip():
        description = _GENERIC_MISSING_INFORMATION
    # Enforce the 1..1000 char bound; trim overly long overrides rather than
    # letting model construction raise.
    return description[:_MAX_MISSING_INFORMATION]


def _select_reason_code(
    *,
    route: str,
    retrieval_scores: Sequence[float] | None,
    retrieval_score_threshold: float,
    confidence_score: float | None,
    route_min_confidence: float,
    claims: Sequence[Claim],
    conflicting_claim_ids: Collection[str] | None,
    sql_row_count: int | None,
    is_material: Callable[[Claim], bool],
) -> ReasonCode | None:
    """Apply the six triggers in fixed precedence, returning at most one code."""
    # --- Pre-generation retrieval gates (only when retrieval was performed) ---
    if retrieval_scores is not None:
        # R3.2: retrieval ran and returned nothing.
        if len(retrieval_scores) == 0:
            return ReasonCode.no_evidence
        # R3.6: every retrieval score is below the configured threshold.
        if all(score < retrieval_score_threshold for score in retrieval_scores):
            return ReasonCode.retrieval_below_threshold

    # --- Post-generation confidence / claim gates ---
    # R3.1: confidence below the route minimum (skipped when no score supplied).
    if confidence_score is not None and confidence_score < route_min_confidence:
        return ReasonCode.low_confidence

    # R3.3: any material claim is unsupported.
    if any(
        claim.evidence_status == EvidenceStatus.unsupported and is_material(claim)
        for claim in claims
    ):
        return ReasonCode.unsupported_claims

    # R3.4: a claim carries conflicting evidence. Honor a pre-computed flag set
    # when provided; otherwise derive the condition from the claims directly.
    if conflicting_claim_ids is not None:
        if len(conflicting_claim_ids) > 0:
            return ReasonCode.conflicting_evidence
    elif any(has_conflicting_evidence(claim) for claim in claims):
        return ReasonCode.conflicting_evidence

    # R3.5: SQL route executed and returned no applicable rows.
    if route == DATABASE_ROUTE and sql_row_count is not None and sql_row_count == 0:
        return ReasonCode.sql_no_rows

    return None


def evaluate_abstention(
    *,
    trace_id: str,
    route: str,
    retrieval_scores: Sequence[float] | None = None,
    retrieval_score_threshold: float = 0.0,
    confidence_score: float | None = None,
    route_min_confidence: float = 0.0,
    claims: Sequence[Claim] | None = None,
    conflicting_claim_ids: Collection[str] | None = None,
    sql_row_count: int | None = None,
    is_material: Callable[[Claim], bool] = default_is_material,
    missing_information: Mapping[ReasonCode, str] | None = None,
) -> AbstentionResponse | None:
    """Evaluate the six abstention triggers for a single query.

    Args:
        trace_id: Identifier of the query trace, echoed on the response.
        route: The selected route (``"rag"``, ``"database"``, or ``"hybrid"``);
            only ``"database"`` enables the ``sql_no_rows`` gate.
        retrieval_scores: Retrieval scores for the question. ``None`` means
            retrieval was not performed (retrieval gates are skipped); an empty
            sequence means retrieval ran and returned no hits (fires
            ``no_evidence``).
        retrieval_score_threshold: Minimum score below which retrieved hits are
            considered too weak to ground an answer (R3.6).
        confidence_score: The answer's confidence in ``[0, 1]``; ``None`` skips
            the ``low_confidence`` gate.
        route_min_confidence: Minimum confidence for the route (R3.1).
        claims: The decomposed claims with their derived ``evidence_status``.
        conflicting_claim_ids: Optional pre-computed set of claim ids flagged as
            carrying conflicting evidence during verification (R1.3). When
            ``None``, the condition is derived directly from ``claims``.
        sql_row_count: Number of rows the SQL route returned; ``0`` fires
            ``sql_no_rows`` for the ``database`` route (R3.5).
        is_material: Predicate deciding whether an unsupported claim is material
            (R3.3). Defaults to treating every claim as material.
        missing_information: Optional per-reason-code overrides for the
            ``missing_information`` description.

    Returns:
        An :class:`AbstentionResponse` with exactly one reason code when a
        trigger fires, or ``None`` when the answer may be returned.
    """
    reason_code = _select_reason_code(
        route=route,
        retrieval_scores=retrieval_scores,
        retrieval_score_threshold=retrieval_score_threshold,
        confidence_score=confidence_score,
        route_min_confidence=route_min_confidence,
        claims=claims or (),
        conflicting_claim_ids=conflicting_claim_ids,
        sql_row_count=sql_row_count,
        is_material=is_material,
    )
    if reason_code is None:
        return None

    return AbstentionResponse(
        reason_code=reason_code,
        missing_information=_resolve_missing_information(reason_code, missing_information),
        trace_id=trace_id,
    )
