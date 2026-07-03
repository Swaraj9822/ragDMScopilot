# Feature: rag-trust-and-observability, Property 3: Evidence status is correctly derived from verification results
"""Property-based test for evidence-status derivation (task 2.2).

Feature: rag-trust-and-observability.

**Property 3: Evidence status is correctly derived from verification results.**

**Validates: Requirements 1.4, 1.5, 1.6, 1.7, 1.8.**

*For any* claim and any sequence of associated ``EvidenceItem``s carrying
arbitrary ``verification_result`` / ``coverage`` combinations (in both
``document`` and ``database`` kinds), :func:`classify_evidence_status` assigns
exactly one ``EvidenceStatus`` such that:

* R1.4 — ``supported`` when some ``entails`` item covers the whole claim
  (``coverage == full``);
* R1.5 — ``partially_supported`` when one or more ``entails`` items cover only
  sub-parts (``coverage == partial``) and no single item is ``full``;
* R1.6 — ``unsupported`` when there is at least one item and every item is
  ``does_not_entail``;
* R1.7 — ``unsupported`` when there are zero evidence items;
* R1.8 — ``verification_unavailable`` when every item is ``undetermined``.

The test compares the implementation's output against an independent oracle of
the documented, fixed precedence for every generated combination, and asserts a
single member of the ``EvidenceStatus`` enum is always returned.
"""

from __future__ import annotations

from collections.abc import Sequence

from hypothesis import given
from hypothesis import strategies as st

from rag_system.claims import classify_evidence_status
from rag_system.models import (
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    VerificationResult,
)

# --- generators ------------------------------------------------------------

_VERIFICATION = st.sampled_from(list(VerificationResult))
_COVERAGE = st.sampled_from(list(EvidenceCoverage))


@st.composite
def _evidence_item(draw: st.DrawFn) -> EvidenceItem:
    """A valid ``EvidenceItem`` of either kind with arbitrary result/coverage.

    The discriminated model requires the per-kind fields, so each kind is built
    with the fields its ``model_validator`` demands; the ``verification_result``
    and ``coverage`` signals — the only inputs ``classify_evidence_status``
    reads — range freely across their full domains.
    """
    verification = draw(_VERIFICATION)
    coverage = draw(_COVERAGE)
    if draw(st.booleans()):
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
    return EvidenceItem(
        kind="database",
        verification_result=verification,
        coverage=coverage,
        table="orders",
        row_fields={"id": 1},
    )


# 0..100 evidence items per claim (R1.2 bound); small upper bound keeps the
# property fast while still exercising empty, single, and multi-item cases.
_evidence_items = st.lists(_evidence_item(), min_size=0, max_size=8)


# --- oracle ----------------------------------------------------------------


def _expected_status(items: Sequence[EvidenceItem]) -> EvidenceStatus:
    """Independent model of the R1.4–R1.8 precedence."""
    # R1.7: zero evidence items.
    if not items:
        return EvidenceStatus.unsupported

    has_full = any(
        i.verification_result == VerificationResult.entails
        and i.coverage == EvidenceCoverage.full
        for i in items
    )
    has_partial = any(
        i.verification_result == VerificationResult.entails
        and i.coverage == EvidenceCoverage.partial
        for i in items
    )
    has_entails = any(i.verification_result == VerificationResult.entails for i in items)
    has_dne = any(
        i.verification_result == VerificationResult.does_not_entail for i in items
    )
    has_undetermined = any(
        i.verification_result == VerificationResult.undetermined for i in items
    )

    # R1.4: some item entails the entire claim.
    if has_full:
        return EvidenceStatus.supported
    # R1.5: entails items cover only sub-parts, none full.
    if has_partial:
        return EvidenceStatus.partially_supported
    # R1.8: every item is undetermined.
    if has_undetermined and not has_entails and not has_dne:
        return EvidenceStatus.verification_unavailable
    # R1.6: at least one item and every item is does_not_entail.
    if has_dne and not has_entails and not has_undetermined:
        return EvidenceStatus.unsupported
    # Remaining mixes carry no whole/partial entailment support → unsupported.
    return EvidenceStatus.unsupported


# --- property --------------------------------------------------------------


@given(items=_evidence_items)
def test_evidence_status_matches_spec_precedence(
    items: list[EvidenceItem],
) -> None:
    status = classify_evidence_status(items)

    # Exactly one status from the defined set is always returned.
    assert isinstance(status, EvidenceStatus)
    assert status == _expected_status(items)


@given(items=_evidence_items)
def test_supported_iff_some_item_fully_entails(
    items: list[EvidenceItem],
) -> None:
    # R1.4: `supported` is derived exactly when some item is `entails` + `full`.
    has_full_entailment = any(
        i.verification_result == VerificationResult.entails
        and i.coverage == EvidenceCoverage.full
        for i in items
    )
    assert (
        classify_evidence_status(items) == EvidenceStatus.supported
    ) == has_full_entailment


@given(items=_evidence_items)
def test_verification_unavailable_iff_all_undetermined(
    items: list[EvidenceItem],
) -> None:
    # R1.8: `verification_unavailable` exactly when there is >=1 item and every
    # item is `undetermined`.
    all_undetermined = bool(items) and all(
        i.verification_result == VerificationResult.undetermined for i in items
    )
    assert (
        classify_evidence_status(items) == EvidenceStatus.verification_unavailable
    ) == all_undetermined
