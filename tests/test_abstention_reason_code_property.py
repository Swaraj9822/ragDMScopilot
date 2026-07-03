# Feature: rag-trust-and-observability, Property 9: Abstention selects exactly the correct reason code
"""Property-based test for reason-code selection in ``evaluate_abstention`` (R3).

Feature: rag-trust-and-observability (task 3.2).

**Property 9: Abstention selects exactly the correct reason code.**

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6.**

Across arbitrary combinations of the answer-path signals (route, retrieval
scores + threshold, confidence + route minimum, decomposed claims with their
evidence status / conflicting evidence, a pre-computed conflict flag set, and a
SQL row count), :func:`evaluate_abstention` must select **at most one**
``ReasonCode`` following a single fixed, deterministic precedence:

1. ``no_evidence`` — retrieval was performed and returned nothing (R3.2).
2. ``retrieval_below_threshold`` — retrieval returned hits but every score is
   below the configured threshold (R3.6).
3. ``low_confidence`` — the confidence score is below the route minimum (R3.1).
4. ``unsupported_claims`` — at least one material claim is ``unsupported`` (R3.3).
5. ``conflicting_evidence`` — a claim carries contradictory evidence (R3.4).
6. ``sql_no_rows`` — the SQL route returned zero rows (R3.5).

The test asserts the implementation's chosen code exactly matches an independent
oracle of that precedence for every generated signal combination, and that when
a code is chosen the response carries exactly that one code.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from hypothesis import given
from hypothesis import strategies as st

from rag_system.abstention import evaluate_abstention, has_conflicting_evidence
from rag_system.models import (
    AnswerSpan,
    Claim,
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    ReasonCode,
    VerificationResult,
)

TRACE_ID = "trace-prop-9"
DATABASE_ROUTE = "database"

# --- generators ------------------------------------------------------------

_ROUTES = st.sampled_from(["rag", "database", "hybrid"])
_SCORE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def _conflicting_items() -> list[EvidenceItem]:
    """Two document items from *different* sources that contradict each other."""
    return [
        EvidenceItem(
            kind="document",
            verification_result=VerificationResult.entails,
            coverage=EvidenceCoverage.partial,
            quote="q",
            source_start=0,
            source_end=1,
            document_id="doc-a",
            document_version="v1",
        ),
        EvidenceItem(
            kind="document",
            verification_result=VerificationResult.does_not_entail,
            coverage=EvidenceCoverage.none,
            quote="q",
            source_start=0,
            source_end=1,
            document_id="doc-b",
            document_version="v1",
        ),
    ]


@st.composite
def _claims(draw: st.DrawFn) -> list[Claim]:
    """Generate a small list of claims with distinct ids.

    Each claim independently draws an ``evidence_status`` and whether it carries
    conflicting evidence, so the ``unsupported_claims`` and ``conflicting_evidence``
    triggers are both exercised, together and in isolation.
    """
    count = draw(st.integers(min_value=0, max_value=4))
    claims: list[Claim] = []
    for index in range(count):
        status = draw(st.sampled_from(list(EvidenceStatus)))
        conflicting = draw(st.booleans())
        items = _conflicting_items() if conflicting else []
        claims.append(
            Claim(
                claim_id=f"c{index}",
                text="a factual claim",
                answer_span=AnswerSpan(start=0, end=1),
                evidence_items=items,
                evidence_status=status,
            )
        )
    return claims


# --- oracle ----------------------------------------------------------------


def _expected_reason_code(
    *,
    route: str,
    retrieval_scores: Sequence[float] | None,
    retrieval_score_threshold: float,
    confidence_score: float | None,
    route_min_confidence: float,
    claims: Sequence[Claim],
    conflicting_claim_ids: Collection[str] | None,
    sql_row_count: int | None,
) -> ReasonCode | None:
    """Independent model of the documented fixed precedence (every claim material)."""
    if retrieval_scores is not None:
        if len(retrieval_scores) == 0:
            return ReasonCode.no_evidence
        if all(score < retrieval_score_threshold for score in retrieval_scores):
            return ReasonCode.retrieval_below_threshold

    if confidence_score is not None and confidence_score < route_min_confidence:
        return ReasonCode.low_confidence

    if any(claim.evidence_status == EvidenceStatus.unsupported for claim in claims):
        return ReasonCode.unsupported_claims

    if conflicting_claim_ids is not None:
        if len(conflicting_claim_ids) > 0:
            return ReasonCode.conflicting_evidence
    elif any(has_conflicting_evidence(claim) for claim in claims):
        return ReasonCode.conflicting_evidence

    if route == DATABASE_ROUTE and sql_row_count is not None and sql_row_count == 0:
        return ReasonCode.sql_no_rows

    return None


# --- property --------------------------------------------------------------


@given(
    route=_ROUTES,
    retrieval_scores=st.one_of(st.none(), st.lists(_SCORE, max_size=6)),
    retrieval_score_threshold=_SCORE,
    confidence_score=st.one_of(st.none(), _SCORE),
    route_min_confidence=_SCORE,
    claims=_claims(),
    use_precomputed_conflict=st.booleans(),
    conflict_flag_nonempty=st.booleans(),
    sql_row_count=st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
)
def test_abstention_selects_exactly_the_correct_reason_code(
    route: str,
    retrieval_scores: list[float] | None,
    retrieval_score_threshold: float,
    confidence_score: float | None,
    route_min_confidence: float,
    claims: list[Claim],
    use_precomputed_conflict: bool,
    conflict_flag_nonempty: bool,
    sql_row_count: int | None,
) -> None:
    # Optionally supply a pre-computed conflict flag set (R1.3 hand-off): when
    # provided it takes precedence over deriving conflict from the claims.
    conflicting_claim_ids: set[str] | None = None
    if use_precomputed_conflict:
        conflicting_claim_ids = {"c0"} if conflict_flag_nonempty else set()

    expected = _expected_reason_code(
        route=route,
        retrieval_scores=retrieval_scores,
        retrieval_score_threshold=retrieval_score_threshold,
        confidence_score=confidence_score,
        route_min_confidence=route_min_confidence,
        claims=claims,
        conflicting_claim_ids=conflicting_claim_ids,
        sql_row_count=sql_row_count,
    )

    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route=route,
        retrieval_scores=retrieval_scores,
        retrieval_score_threshold=retrieval_score_threshold,
        confidence_score=confidence_score,
        route_min_confidence=route_min_confidence,
        claims=claims,
        conflicting_claim_ids=conflicting_claim_ids,
        sql_row_count=sql_row_count,
    )

    if expected is None:
        assert result is None
    else:
        assert result is not None
        # Exactly one reason code is selected, and it is the correct one per the
        # fixed precedence.
        assert result.reason_code == expected
        assert result.trace_id == TRACE_ID
