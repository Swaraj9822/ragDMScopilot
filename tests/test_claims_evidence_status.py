"""Unit tests for `classify_evidence_status` (R1.4–R1.8)."""

from __future__ import annotations

from rag_system.claims import classify_evidence_status
from rag_system.models import (
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    VerificationResult,
)


def _doc_item(
    verification: VerificationResult,
    coverage: EvidenceCoverage,
) -> EvidenceItem:
    return EvidenceItem(
        kind="document",
        verification_result=verification,
        coverage=coverage,
        quote="q",
        source_start=0,
        source_end=1,
        document_id="doc-1",
        document_version="v1",
    )


def test_zero_items_is_unsupported():
    # R1.7
    assert classify_evidence_status([]) == EvidenceStatus.unsupported


def test_full_entailment_is_supported():
    # R1.4
    items = [_doc_item(VerificationResult.entails, EvidenceCoverage.full)]
    assert classify_evidence_status(items) == EvidenceStatus.supported


def test_full_entailment_wins_over_partial():
    # R1.4 takes precedence over R1.5 when any item is full.
    items = [
        _doc_item(VerificationResult.entails, EvidenceCoverage.partial),
        _doc_item(VerificationResult.entails, EvidenceCoverage.full),
    ]
    assert classify_evidence_status(items) == EvidenceStatus.supported


def test_partial_only_is_partially_supported():
    # R1.5
    items = [
        _doc_item(VerificationResult.entails, EvidenceCoverage.partial),
        _doc_item(VerificationResult.does_not_entail, EvidenceCoverage.none),
    ]
    assert classify_evidence_status(items) == EvidenceStatus.partially_supported


def test_all_does_not_entail_is_unsupported():
    # R1.6
    items = [
        _doc_item(VerificationResult.does_not_entail, EvidenceCoverage.none),
        _doc_item(VerificationResult.does_not_entail, EvidenceCoverage.none),
    ]
    assert classify_evidence_status(items) == EvidenceStatus.unsupported


def test_all_undetermined_is_verification_unavailable():
    # R1.8
    items = [
        _doc_item(VerificationResult.undetermined, EvidenceCoverage.none),
        _doc_item(VerificationResult.undetermined, EvidenceCoverage.none),
    ]
    assert (
        classify_evidence_status(items)
        == EvidenceStatus.verification_unavailable
    )


def test_mixed_does_not_entail_and_undetermined_is_unsupported():
    # Not uniformly undetermined (R1.8) nor uniformly does_not_entail (R1.6);
    # with no entailment support the claim defaults to unsupported.
    items = [
        _doc_item(VerificationResult.does_not_entail, EvidenceCoverage.none),
        _doc_item(VerificationResult.undetermined, EvidenceCoverage.none),
    ]
    assert classify_evidence_status(items) == EvidenceStatus.unsupported


def test_entails_with_no_coverage_is_unsupported():
    # A degenerate entails item that covers nothing does not confer support.
    items = [_doc_item(VerificationResult.entails, EvidenceCoverage.none)]
    assert classify_evidence_status(items) == EvidenceStatus.unsupported


def test_database_evidence_supported():
    item = EvidenceItem(
        kind="database",
        verification_result=VerificationResult.entails,
        coverage=EvidenceCoverage.full,
        table="orders",
        row_fields={"id": 1},
    )
    assert classify_evidence_status([item]) == EvidenceStatus.supported
