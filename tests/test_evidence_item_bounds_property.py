# Feature: rag-trust-and-observability, Property 2: Evidence items are well-formed and bounded
"""Property-based test for evidence-item bounds and shape (task 2.5).

Feature: rag-trust-and-observability.

**Property 2: Evidence items are well-formed and bounded.**

**Validates: Requirements 1.2, 1.3.**

*For any* answer decomposed into claims and *any* set of retrieved hits, every
``EvidenceItem`` :class:`ClaimMapper` associates with a claim is well-formed and
bounded:

* R1.2 — each claim carries between 0 and 100 evidence items (exactly
  ``min(len(hits), 100)`` for the document-passage path);
* R1.2 — a ``document`` item satisfies its per-kind validator (exact quote,
  ``source_start``/``source_end`` offsets within the quote, ``document_id`` and
  ``document_version``); a ``database`` item satisfies its per-kind validator
  (``table`` + ``row_fields``);
* R1.3 — every item records a ``verification_result`` and a ``coverage`` signal,
  regardless of whether the entailment model succeeded, raised, or returned
  unparseable output (the fail-closed path still yields ``undetermined`` /
  ``none``).

Model outputs are stubbed for determinism (the stubbed-LLM pattern from
``tests/test_claim_mapper.py``) so the mapping is exercised without any live
model call.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from rag_system.claims import MAX_EVIDENCE_ITEMS, ClaimMapper
from rag_system.models import (
    Chunk,
    EvidenceCoverage,
    EvidenceItem,
    RetrievalHit,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Test doubles (stubbed-LLM pattern from tests/test_claim_mapper.py)
# ---------------------------------------------------------------------------


class _StubLLM:
    """Fake ``TextLLM``: canned decomposition + a configurable verdict path.

    Args:
        decomposition: The list of ``{"text", "start", "end"}`` dicts the
            decomposition call returns.
        verdict: The ``(verification_result, coverage)`` string tuple returned
            for every entailment call in the ``"normal"`` mode.
        mode: ``"normal"`` returns ``verdict``; ``"raise"`` raises on every
            entailment call (fail-closed to ``undetermined``/``none``);
            ``"garbage"`` returns unparseable output (also fail-closed).
    """

    model_id = "stub-generator"
    provider = "stub"

    def __init__(self, decomposition, verdict, *, mode: str = "normal") -> None:
        self._decomposition = decomposition
        self._verdict = verdict
        self._mode = mode

    def generate(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        if "Decompose the following answer" in prompt:
            return json.dumps({"claims": self._decomposition}), {}
        # Otherwise it is a per-pair entailment prompt.
        if self._mode == "raise":
            raise RuntimeError("simulated entailment model error")
        if self._mode == "garbage":
            return "not json at all", {}
        result, coverage = self._verdict
        return (
            json.dumps(
                {
                    "verification_result": result,
                    "coverage": coverage,
                    "covered_subclaims": [0],
                }
            ),
            {},
        )

    def generate_stream(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        yield ""


def _mapper(llm: _StubLLM) -> ClaimMapper:
    mapper = object.__new__(ClaimMapper)
    mapper._llm = llm
    mapper._model_id = llm.model_id
    return mapper


def _hit(chunk_id: str, document_id: str, version: str, text: str) -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(
            id=chunk_id,
            document_id=document_id,
            version=version,
            text=text,
            page_start=1,
            page_end=1,
        ),
        score=0.9,
        source="test",
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_RESULT = st.sampled_from([r.value for r in VerificationResult])
_COVERAGE = st.sampled_from([c.value for c in EvidenceCoverage])
_MODE = st.sampled_from(["normal", "raise", "garbage"])

# A decomposition entry with a non-empty statement and arbitrary (even
# out-of-range) offsets — the mapper clamps them, and Property 2 only concerns
# the associated evidence items, not the answer span.
_decomp_entry = st.fixed_dictionaries(
    {
        "text": st.text(min_size=1, max_size=24).filter(lambda s: s.strip()),
        "start": st.integers(min_value=-5, max_value=60),
        "end": st.integers(min_value=-5, max_value=60),
    }
)
_decomposition = st.lists(_decomp_entry, min_size=0, max_size=5)

# 0..(MAX+a few) hit specs so the 100-item cap (R1.2) is exercised.
_hit_spec = st.fixed_dictionaries(
    {
        "chunk_id": st.text(min_size=1, max_size=8),
        "document_id": st.text(min_size=1, max_size=8),
        "version": st.text(min_size=1, max_size=6),
        "text": st.text(min_size=0, max_size=40),
    }
)
_hit_specs = st.lists(_hit_spec, min_size=0, max_size=MAX_EVIDENCE_ITEMS + 5)

_ANSWER = "The revenue grew and the margins improved over the period."


# ---------------------------------------------------------------------------
# Property: mapped document evidence is well-formed and bounded (R1.2, R1.3)
# ---------------------------------------------------------------------------


@given(
    decomposition=_decomposition,
    hit_specs=_hit_specs,
    result=_RESULT,
    coverage=_COVERAGE,
    mode=_MODE,
)
def test_map_claims_evidence_is_well_formed_and_bounded(
    decomposition: list[dict],
    hit_specs: list[dict],
    result: str,
    coverage: str,
    mode: str,
) -> None:
    llm = _StubLLM(decomposition, (result, coverage), mode=mode)
    mapper = _mapper(llm)
    hits = [_hit(**spec) for spec in hit_specs]

    outcome = mapper.map_claims(_ANSWER, hits, "trace-1")

    # A well-formed (possibly empty) decomposition is never a failure.
    assert outcome.decomposition_failed is False

    expected_count = min(len(hits), MAX_EVIDENCE_ITEMS)
    for claim in outcome.claims:
        items = claim.evidence_items
        # R1.2: between 0 and 100 evidence items, one per candidate hit up to
        # the cap.
        assert 0 <= len(items) <= MAX_EVIDENCE_ITEMS
        assert len(items) == expected_count

        for item in items:
            # The mapper draws candidate evidence from document passages, so
            # each item is a well-formed `document` kind (its per-kind validator
            # ran at construction).
            assert item.kind == "document"
            # Document per-kind fields (R1.2).
            assert item.quote is not None
            assert item.document_id is not None
            assert item.document_version is not None
            assert item.source_start is not None
            assert item.source_end is not None
            # Offsets lie within the quote: 0 <= start <= end <= len(quote).
            assert 0 <= item.source_start <= item.source_end <= len(item.quote)
            # R1.3: a verification_result and coverage are always present, even
            # on the fail-closed (raise/garbage) path.
            assert isinstance(item.verification_result, VerificationResult)
            assert isinstance(item.coverage, EvidenceCoverage)

    # The fail-closed paths must degrade every item to undetermined/none (R1.3).
    if mode in {"raise", "garbage"}:
        for claim in outcome.claims:
            for item in claim.evidence_items:
                assert item.verification_result == VerificationResult.undetermined
                assert item.coverage == EvidenceCoverage.none


# ---------------------------------------------------------------------------
# Property: both kinds satisfy their per-kind validator (R1.2, R1.3)
# ---------------------------------------------------------------------------


@st.composite
def _valid_document_item(draw: st.DrawFn) -> EvidenceItem:
    quote = draw(st.text(min_size=0, max_size=30))
    start = draw(st.integers(min_value=0, max_value=len(quote)))
    end = draw(st.integers(min_value=start, max_value=len(quote)))
    return EvidenceItem(
        kind="document",
        verification_result=draw(st.sampled_from(list(VerificationResult))),
        coverage=draw(st.sampled_from(list(EvidenceCoverage))),
        quote=quote,
        source_start=start,
        source_end=end,
        document_id=draw(st.text(min_size=1, max_size=8)),
        document_version=draw(st.text(min_size=1, max_size=6)),
    )


@st.composite
def _valid_database_item(draw: st.DrawFn) -> EvidenceItem:
    return EvidenceItem(
        kind="database",
        verification_result=draw(st.sampled_from(list(VerificationResult))),
        coverage=draw(st.sampled_from(list(EvidenceCoverage))),
        table=draw(st.text(min_size=1, max_size=12)),
        row_fields=draw(
            st.dictionaries(
                keys=st.text(min_size=1, max_size=6),
                values=st.integers() | st.text(max_size=8),
                max_size=4,
            )
        ),
    )


@given(item=st.one_of(_valid_document_item(), _valid_database_item()))
def test_evidence_item_of_both_kinds_is_well_formed(item: EvidenceItem) -> None:
    # R1.3: verification_result + coverage are always present on any item.
    assert isinstance(item.verification_result, VerificationResult)
    assert isinstance(item.coverage, EvidenceCoverage)

    if item.kind == "document":
        # Document per-kind validator fields (R1.2).
        assert item.quote is not None
        assert item.source_start is not None and item.source_end is not None
        assert item.document_id is not None and item.document_version is not None
    else:
        # Database per-kind validator fields (R1.2).
        assert item.kind == "database"
        assert item.table is not None
        assert item.row_fields is not None


@given(
    result=st.sampled_from(list(VerificationResult)),
    coverage=st.sampled_from(list(EvidenceCoverage)),
)
def test_per_kind_validator_rejects_missing_required_fields(
    result: VerificationResult, coverage: EvidenceCoverage
) -> None:
    # A `document` item missing any required per-kind field is rejected (R1.2).
    try:
        EvidenceItem(
            kind="document",
            verification_result=result,
            coverage=coverage,
            quote="q",
            source_start=0,
            source_end=1,
            document_id="doc-1",
            # document_version omitted → invalid
        )
        raise AssertionError("expected a validation error for the document item")
    except ValidationError:
        pass

    # A `database` item missing table/row_fields is rejected (R1.2).
    try:
        EvidenceItem(
            kind="database",
            verification_result=result,
            coverage=coverage,
            table="orders",
            # row_fields omitted → invalid
        )
        raise AssertionError("expected a validation error for the database item")
    except ValidationError:
        pass
