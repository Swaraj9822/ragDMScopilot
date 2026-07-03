# Feature: rag-trust-and-observability, Property: Claim/EvidenceItem serialization round-trip

"""Property-based test for Claim/EvidenceItem serialization round-trip (task 2.8).

**Validates: Requirements 1.14**

Verifies that serializing a Claim (with embedded EvidenceItems of both document
and database kinds, plus coverage/covered_subclaims) via model_dump() and then
deserializing via model_validate() preserves all fields exactly. This ensures
the discriminated union (document vs database) and nested structures survive
JSON serialization intact.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.models import (
    AnswerSpan,
    Claim,
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    VerificationResult,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_verification_results = st.sampled_from(list(VerificationResult))
_coverages = st.sampled_from(list(EvidenceCoverage))
_evidence_statuses = st.sampled_from(list(EvidenceStatus))
_subclaims = st.lists(st.integers(min_value=0, max_value=50), max_size=5)


@st.composite
def document_evidence_items(draw: st.DrawFn) -> EvidenceItem:
    """Generate a valid document-kind EvidenceItem."""
    start = draw(st.integers(min_value=0, max_value=500))
    end = draw(st.integers(min_value=start, max_value=start + 500))
    return EvidenceItem(
        kind="document",
        verification_result=draw(_verification_results),
        coverage=draw(_coverages),
        covered_subclaims=draw(_subclaims),
        quote=draw(st.text(min_size=1, max_size=80)),
        source_start=start,
        source_end=end,
        document_id=draw(st.text(min_size=1, max_size=20)),
        document_version=draw(st.text(min_size=1, max_size=12)),
    )


@st.composite
def database_evidence_items(draw: st.DrawFn) -> EvidenceItem:
    """Generate a valid database-kind EvidenceItem."""
    keys = draw(
        st.lists(st.text(min_size=1, max_size=10), min_size=1, max_size=5, unique=True)
    )
    row_fields = {k: draw(st.one_of(st.integers(-1000, 1000), st.text(max_size=20))) for k in keys}
    return EvidenceItem(
        kind="database",
        verification_result=draw(_verification_results),
        coverage=draw(_coverages),
        covered_subclaims=draw(_subclaims),
        table=draw(st.text(min_size=1, max_size=20)),
        row_fields=row_fields,
        sql=draw(st.one_of(st.none(), st.text(min_size=1, max_size=50))),
        sql_query_id=draw(st.one_of(st.none(), st.text(min_size=1, max_size=20))),
        sql_result_fixture_id=draw(st.one_of(st.none(), st.text(min_size=1, max_size=20))),
        row_index=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=1000))),
    )


@st.composite
def mixed_evidence_lists(draw: st.DrawFn) -> list[EvidenceItem]:
    """Generate a list containing at least one document and one database item."""
    doc_items = draw(st.lists(document_evidence_items(), min_size=1, max_size=3))
    db_items = draw(st.lists(database_evidence_items(), min_size=1, max_size=3))
    all_items = doc_items + db_items
    # Shuffle to vary order
    draw(st.randoms()).shuffle(all_items)
    return all_items


@st.composite
def claims_with_mixed_evidence(draw: st.DrawFn) -> Claim:
    """Generate a Claim with embedded EvidenceItems of both document and database kinds."""
    text = draw(st.text(min_size=1, max_size=200))
    start = draw(st.integers(min_value=0, max_value=100))
    end = draw(st.integers(min_value=start, max_value=start + len(text)))
    evidence = draw(mixed_evidence_lists())
    return Claim(
        claim_id=draw(st.text(min_size=1, max_size=30)),
        text=text,
        answer_span=AnswerSpan(start=start, end=end),
        evidence_items=evidence,
        evidence_status=draw(_evidence_statuses),
    )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property: Claim/EvidenceItem serialization round-trip
# Validates: Requirements 1.14
@settings(max_examples=200)
@given(claim=claims_with_mixed_evidence())
def test_claim_with_mixed_evidence_roundtrip(claim: Claim):
    """Serialize then deserialize a Claim preserves all fields exactly."""
    dumped = claim.model_dump()
    restored = Claim.model_validate(dumped)
    assert restored == claim

    # Verify structural invariants survived
    assert restored.claim_id == claim.claim_id
    assert restored.text == claim.text
    assert restored.answer_span == claim.answer_span
    assert restored.evidence_status == claim.evidence_status
    assert len(restored.evidence_items) == len(claim.evidence_items)

    # Verify both kinds are present
    kinds = {item.kind for item in restored.evidence_items}
    assert "document" in kinds
    assert "database" in kinds


# Feature: rag-trust-and-observability, Property: Claim/EvidenceItem serialization round-trip
# Validates: Requirements 1.14
@settings(max_examples=200)
@given(claim=claims_with_mixed_evidence())
def test_discriminated_union_kind_preserved(claim: Claim):
    """The discriminated union (document vs database) round-trips correctly."""
    dumped = claim.model_dump()
    restored = Claim.model_validate(dumped)

    for original, roundtripped in zip(claim.evidence_items, restored.evidence_items):
        assert roundtripped.kind == original.kind
        if original.kind == "document":
            assert roundtripped.quote == original.quote
            assert roundtripped.source_start == original.source_start
            assert roundtripped.source_end == original.source_end
            assert roundtripped.document_id == original.document_id
            assert roundtripped.document_version == original.document_version
        else:
            assert roundtripped.table == original.table
            assert roundtripped.row_fields == original.row_fields
            assert roundtripped.sql == original.sql
            assert roundtripped.sql_query_id == original.sql_query_id
            assert roundtripped.sql_result_fixture_id == original.sql_result_fixture_id
            assert roundtripped.row_index == original.row_index


# Feature: rag-trust-and-observability, Property: Claim/EvidenceItem serialization round-trip
# Validates: Requirements 1.14
@settings(max_examples=200)
@given(claim=claims_with_mixed_evidence())
def test_coverage_and_subclaims_preserved(claim: Claim):
    """Coverage values (full/partial/none) and covered_subclaims lists survive serialization."""
    dumped = claim.model_dump()
    restored = Claim.model_validate(dumped)

    for original, roundtripped in zip(claim.evidence_items, restored.evidence_items):
        assert roundtripped.coverage == original.coverage
        assert roundtripped.covered_subclaims == original.covered_subclaims
        assert roundtripped.verification_result == original.verification_result


# Feature: rag-trust-and-observability, Property: Claim/EvidenceItem serialization round-trip
# Validates: Requirements 1.14
@settings(max_examples=200)
@given(claim=claims_with_mixed_evidence())
def test_json_roundtrip_preserves_all_fields(claim: Claim):
    """JSON string round-trip (model_dump_json → model_validate_json) preserves all fields."""
    json_str = claim.model_dump_json()
    restored = Claim.model_validate_json(json_str)
    assert restored == claim
