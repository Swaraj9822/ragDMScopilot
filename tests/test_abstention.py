"""Unit tests for the evidence-based abstention gate (R3).

Feature: rag-trust-and-observability (task 3.1 — ``evaluate_abstention``).

These exercise each of the six triggers, the fixed precedence between them, the
route-appropriateness of the retrieval and SQL gates, the materiality predicate,
the conflicting-evidence derivation, and the response-shape guarantees (exactly
one reason code, a bounded 1..1000 char description, no answer content). The
numbered correctness properties (Properties 9 and 10) live in their own tasks.
"""

from __future__ import annotations

from rag_system.abstention import (
    DEFAULT_MISSING_INFORMATION,
    evaluate_abstention,
    has_conflicting_evidence,
)
from rag_system.models import (
    AnswerSpan,
    Claim,
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    ReasonCode,
    VerificationResult,
)

TRACE_ID = "trace-123"


def _doc_evidence(
    *,
    verification_result: VerificationResult,
    document_id: str = "doc-1",
    coverage: EvidenceCoverage = EvidenceCoverage.full,
) -> EvidenceItem:
    return EvidenceItem(
        kind="document",
        verification_result=verification_result,
        coverage=coverage,
        quote="q",
        source_start=0,
        source_end=1,
        document_id=document_id,
        document_version="v1",
    )


def _claim(
    *,
    claim_id: str = "c1",
    evidence_status: EvidenceStatus,
    evidence_items: list[EvidenceItem] | None = None,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        text="a claim",
        answer_span=AnswerSpan(start=0, end=1),
        evidence_items=evidence_items or [],
        evidence_status=evidence_status,
    )


# --- individual triggers ---------------------------------------------------


def test_no_evidence_when_retrieval_empty():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[],
        retrieval_score_threshold=0.3,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.no_evidence


def test_retrieval_below_threshold_when_all_scores_low():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.1, 0.2, 0.29],
        retrieval_score_threshold=0.3,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.retrieval_below_threshold


def test_no_abstention_when_a_score_meets_threshold():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.1, 0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[_claim(evidence_status=EvidenceStatus.supported)],
    )
    assert result is None


def test_low_confidence_trigger():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.4,
        route_min_confidence=0.5,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.low_confidence


def test_unsupported_claims_trigger():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[
            _claim(claim_id="c1", evidence_status=EvidenceStatus.supported),
            _claim(claim_id="c2", evidence_status=EvidenceStatus.unsupported),
        ],
    )
    assert result is not None
    assert result.reason_code == ReasonCode.unsupported_claims


def test_unsupported_claims_suppressed_when_not_material():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[_claim(evidence_status=EvidenceStatus.unsupported)],
        is_material=lambda claim: False,
    )
    assert result is None


def test_conflicting_evidence_derived_from_claims():
    claim = _claim(
        evidence_status=EvidenceStatus.partially_supported,
        evidence_items=[
            _doc_evidence(
                verification_result=VerificationResult.entails, document_id="doc-a"
            ),
            _doc_evidence(
                verification_result=VerificationResult.does_not_entail,
                document_id="doc-b",
                coverage=EvidenceCoverage.none,
            ),
        ],
    )
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[claim],
    )
    assert result is not None
    assert result.reason_code == ReasonCode.conflicting_evidence


def test_conflicting_evidence_from_precomputed_flag():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[_claim(evidence_status=EvidenceStatus.partially_supported)],
        conflicting_claim_ids={"c1"},
    )
    assert result is not None
    assert result.reason_code == ReasonCode.conflicting_evidence


def test_precomputed_empty_flag_suppresses_derivation():
    # A non-None but empty flag set means verification found no conflict, and it
    # takes precedence over deriving from the claims.
    claim = _claim(
        evidence_status=EvidenceStatus.partially_supported,
        evidence_items=[
            _doc_evidence(
                verification_result=VerificationResult.entails, document_id="doc-a"
            ),
            _doc_evidence(
                verification_result=VerificationResult.does_not_entail,
                document_id="doc-b",
                coverage=EvidenceCoverage.none,
            ),
        ],
    )
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[claim],
        conflicting_claim_ids=set(),
    )
    assert result is None


def test_sql_no_rows_trigger_for_database_route():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="database",
        sql_row_count=0,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.sql_no_rows


def test_sql_no_rows_not_fired_for_non_database_route():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        sql_row_count=0,
    )
    assert result is None


def test_database_route_with_rows_does_not_abstain():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="database",
        sql_row_count=5,
    )
    assert result is None


# --- precedence ------------------------------------------------------------


def test_no_evidence_precedes_retrieval_below_threshold():
    # Empty retrieval is checked before the below-threshold gate.
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[],
        retrieval_score_threshold=0.3,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.no_evidence


def test_retrieval_gate_precedes_low_confidence():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.1],
        retrieval_score_threshold=0.3,
        confidence_score=0.0,
        route_min_confidence=0.5,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.retrieval_below_threshold


def test_low_confidence_precedes_unsupported_claims():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.4,
        route_min_confidence=0.5,
        claims=[_claim(evidence_status=EvidenceStatus.unsupported)],
    )
    assert result is not None
    assert result.reason_code == ReasonCode.low_confidence


def test_unsupported_claims_precedes_conflicting_evidence():
    conflicting_claim = _claim(
        claim_id="c-conflict",
        evidence_status=EvidenceStatus.partially_supported,
        evidence_items=[
            _doc_evidence(
                verification_result=VerificationResult.entails, document_id="doc-a"
            ),
            _doc_evidence(
                verification_result=VerificationResult.does_not_entail,
                document_id="doc-b",
                coverage=EvidenceCoverage.none,
            ),
        ],
    )
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.9,
        route_min_confidence=0.5,
        claims=[
            _claim(claim_id="c-unsupported", evidence_status=EvidenceStatus.unsupported),
            conflicting_claim,
        ],
    )
    assert result is not None
    assert result.reason_code == ReasonCode.unsupported_claims


# --- retrieval-gate route-appropriateness ----------------------------------


def test_retrieval_gates_skipped_when_scores_none():
    # None => retrieval not performed (e.g. database route); gates are skipped.
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="database",
        retrieval_scores=None,
        sql_row_count=0,
    )
    assert result is not None
    assert result.reason_code == ReasonCode.sql_no_rows


# --- response shape (R3.7, R3.8) -------------------------------------------


def test_response_shape_no_answer_and_bounded_description():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[],
        retrieval_score_threshold=0.3,
    )
    assert result is not None
    assert result.trace_id == TRACE_ID
    assert 1 <= len(result.missing_information) <= 1000
    # No answer / claims / evidence fields exist on the model.
    dumped = result.model_dump()
    assert set(dumped) == {"reason_code", "missing_information", "trace_id"}


def test_description_override_is_used_and_clamped():
    long_override = "x" * 5000
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[],
        retrieval_score_threshold=0.3,
        missing_information={ReasonCode.no_evidence: long_override},
    )
    assert result is not None
    assert len(result.missing_information) == 1000


def test_empty_override_falls_back_to_default():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[],
        retrieval_score_threshold=0.3,
        missing_information={ReasonCode.no_evidence: "   "},
    )
    assert result is not None
    assert len(result.missing_information) >= 1


def test_default_missing_information_covers_all_reason_codes():
    for code in ReasonCode:
        assert code in DEFAULT_MISSING_INFORMATION
        assert 1 <= len(DEFAULT_MISSING_INFORMATION[code]) <= 1000


# --- has_conflicting_evidence helper ---------------------------------------


def test_has_conflicting_evidence_requires_different_documents():
    same_doc = _claim(
        evidence_status=EvidenceStatus.partially_supported,
        evidence_items=[
            _doc_evidence(
                verification_result=VerificationResult.entails, document_id="doc-a"
            ),
            _doc_evidence(
                verification_result=VerificationResult.does_not_entail,
                document_id="doc-a",
                coverage=EvidenceCoverage.none,
            ),
        ],
    )
    assert has_conflicting_evidence(same_doc) is False


def test_has_conflicting_evidence_true_across_documents():
    claim = _claim(
        evidence_status=EvidenceStatus.partially_supported,
        evidence_items=[
            _doc_evidence(
                verification_result=VerificationResult.entails, document_id="doc-a"
            ),
            _doc_evidence(
                verification_result=VerificationResult.does_not_entail,
                document_id="doc-b",
                coverage=EvidenceCoverage.none,
            ),
        ],
    )
    assert has_conflicting_evidence(claim) is True


def test_no_abstention_when_all_clear():
    result = evaluate_abstention(
        trace_id=TRACE_ID,
        route="rag",
        retrieval_scores=[0.8, 0.9],
        retrieval_score_threshold=0.3,
        confidence_score=0.95,
        route_min_confidence=0.5,
        claims=[_claim(evidence_status=EvidenceStatus.supported)],
        sql_row_count=None,
    )
    assert result is None
