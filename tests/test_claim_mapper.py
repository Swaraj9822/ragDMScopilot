"""Unit tests for :class:`ClaimMapper` (task 2.3, R1.1, R1.2, R1.3, R3.4).

Model outputs are stubbed for determinism: a fake LLM inspects the prompt to
distinguish the decomposition call from per-pair entailment calls and returns
canned JSON, so decomposition, evidence association, verification, and
conflicting-evidence detection are exercised without any live model call.
"""

from __future__ import annotations

import json

from rag_system.claims import (
    ClaimMapper,
    ClaimMappingResult,
    derive_claim_id,
)
from rag_system.models import (
    Chunk,
    EvidenceCoverage,
    EvidenceStatus,
    RetrievalHit,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubLLM:
    """Fake TextLLM: routes on prompt content to canned JSON responses.

    Args:
        decomposition: The list of ``{"text", "start", "end"}`` dicts the
            decomposition call returns. ``None`` raises (simulates a model
            error). A non-JSON ``str`` is returned verbatim (unparseable path).
        verifications: Maps ``(claim_substring, quote_substring)`` to a
            ``(verification_result, coverage)`` tuple used when both substrings
            appear in an entailment prompt. Unmatched pairs default to
            ``undetermined``/``none``.
        raise_on_verify: When ``True``, entailment calls raise.
    """

    model_id = "stub-generator"
    provider = "stub"

    def __init__(
        self,
        decomposition=None,
        verifications=None,
        *,
        raise_on_verify: bool = False,
        verify_returns_garbage: bool = False,
    ) -> None:
        self._decomposition = decomposition
        self._verifications = verifications or {}
        self._raise_on_verify = raise_on_verify
        self._verify_returns_garbage = verify_returns_garbage

    def generate(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        if "Decompose the following answer" in prompt:
            if self._decomposition is None:
                raise RuntimeError("simulated decomposition model error")
            if isinstance(self._decomposition, str):
                return self._decomposition, {}
            return json.dumps({"claims": self._decomposition}), {}

        # Otherwise it is an entailment prompt.
        if self._raise_on_verify:
            raise RuntimeError("simulated entailment model error")
        if self._verify_returns_garbage:
            return "not json at all", {}
        for (claim_sub, quote_sub), (result, coverage) in self._verifications.items():
            if claim_sub in prompt and quote_sub in prompt:
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
        return (
            json.dumps({"verification_result": "undetermined", "coverage": "none"}),
            {},
        )

    def generate_stream(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        yield ""


def _mapper(llm: _StubLLM) -> ClaimMapper:
    mapper = object.__new__(ClaimMapper)
    mapper._llm = llm
    mapper._model_id = llm.model_id
    return mapper


def _hit(chunk_id: str, document_id: str, text: str) -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(
            id=chunk_id,
            document_id=document_id,
            version="v1",
            text=text,
            page_start=1,
            page_end=1,
        ),
        score=0.9,
        source="test",
    )


# ---------------------------------------------------------------------------
# Decomposition + claim structure (R1.1)
# ---------------------------------------------------------------------------


def test_decompose_produces_claims_with_stable_ids_and_spans():
    answer = "Revenue was 10. Margin was 20."
    llm = _StubLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
            {"text": "Margin was 20.", "start": 16, "end": 30},
        ],
        verifications={
            ("Revenue was 10.", "Revenue was 10"): ("entails", "full"),
            ("Margin was 20.", "Margin was 20"): ("entails", "full"),
        },
    )
    mapper = _mapper(llm)

    result = mapper.map_claims(
        answer,
        [_hit("c1", "doc-1", "Revenue was 10 last year.")],
        "trace-1",
    )

    assert isinstance(result, ClaimMappingResult)
    assert result.decomposition_failed is False
    assert [c.text for c in result.claims] == ["Revenue was 10.", "Margin was 20."]
    # Stable ids derived from (trace_id, claim_index) (R1.1).
    assert result.claims[0].claim_id == derive_claim_id("trace-1", 0)
    assert result.claims[1].claim_id == derive_claim_id("trace-1", 1)
    # Spans are zero-based within the answer bounds.
    span = result.claims[0].answer_span
    assert 0 <= span.start <= span.end <= len(answer)


def test_claim_ids_are_stable_across_reruns():
    llm = _StubLLM(decomposition=[{"text": "A fact.", "start": 0, "end": 7}])
    first = _mapper(llm).map_claims("A fact.", [], "trace-42")
    second = _mapper(llm).map_claims("A fact.", [], "trace-42")
    assert first.claims[0].claim_id == second.claims[0].claim_id


def test_span_offsets_are_clamped_to_answer_bounds():
    answer = "short"
    llm = _StubLLM(decomposition=[{"text": "short", "start": -5, "end": 999}])
    result = _mapper(llm).map_claims(answer, [], "trace-1")
    span = result.claims[0].answer_span
    assert span.start == 0
    assert span.end == len(answer)


# ---------------------------------------------------------------------------
# Decomposition failure path (R1.9)
# ---------------------------------------------------------------------------


def test_decomposition_error_yields_empty_claims_and_flag():
    llm = _StubLLM(decomposition=None)  # raises on decompose
    result = _mapper(llm).map_claims("anything", [_hit("c1", "doc-1", "x")], "t")
    assert result.claims == []
    assert result.decomposition_failed is True
    assert result.conflicting_claim_ids == set()


def test_decomposition_unparseable_yields_empty_claims_and_flag():
    llm = _StubLLM(decomposition="not valid json")
    result = _mapper(llm).map_claims("anything", [], "t")
    assert result.claims == []
    assert result.decomposition_failed is True


def test_empty_but_well_formed_decomposition_is_not_a_failure():
    # An answer with no factual statements decomposes to zero claims but is not
    # a decomposition failure.
    llm = _StubLLM(decomposition=[])
    result = _mapper(llm).map_claims("Hello!", [], "t")
    assert result.claims == []
    assert result.decomposition_failed is False


# ---------------------------------------------------------------------------
# Evidence association + verification (R1.2, R1.3)
# ---------------------------------------------------------------------------


def test_evidence_items_are_document_kind_and_carry_verdicts():
    llm = _StubLLM(
        decomposition=[{"text": "Revenue was 10.", "start": 0, "end": 15}],
        verifications={("Revenue was 10.", "Revenue"): ("entails", "full")},
    )
    result = _mapper(llm).map_claims(
        "Revenue was 10.",
        [_hit("c1", "doc-1", "Revenue was 10 last year.")],
        "trace-1",
    )
    claim = result.claims[0]
    assert len(claim.evidence_items) == 1
    item = claim.evidence_items[0]
    assert item.kind == "document"
    assert item.document_id == "doc-1"
    assert item.document_version == "v1"
    assert item.quote == "Revenue was 10 last year."
    assert item.source_start == 0
    assert item.source_end == len(item.quote)
    assert item.verification_result == VerificationResult.entails
    assert item.coverage == EvidenceCoverage.full
    # Derived status uses classify_evidence_status (task 2.1 reuse).
    assert claim.evidence_status == EvidenceStatus.supported


def test_partial_coverage_yields_partially_supported():
    llm = _StubLLM(
        decomposition=[{"text": "Revenue rose 10% in Q1.", "start": 0, "end": 23}],
        verifications={("Revenue rose", "Revenue rose"): ("entails", "partial")},
    )
    result = _mapper(llm).map_claims(
        "Revenue rose 10% in Q1.",
        [_hit("c1", "doc-1", "Revenue rose overall.")],
        "trace-1",
    )
    assert result.claims[0].evidence_status == EvidenceStatus.partially_supported


def test_zero_hits_yields_unsupported_claim():
    llm = _StubLLM(decomposition=[{"text": "A fact.", "start": 0, "end": 7}])
    result = _mapper(llm).map_claims("A fact.", [], "trace-1")
    claim = result.claims[0]
    assert claim.evidence_items == []
    assert claim.evidence_status == EvidenceStatus.unsupported


def test_evidence_items_capped_at_100():
    hits = [_hit(f"c{i}", "doc-1", f"passage {i}") for i in range(150)]
    llm = _StubLLM(decomposition=[{"text": "A fact.", "start": 0, "end": 7}])
    result = _mapper(llm).map_claims("A fact.", hits, "trace-1")
    assert len(result.claims[0].evidence_items) == 100


def test_verification_error_degrades_to_undetermined():
    llm = _StubLLM(
        decomposition=[{"text": "A fact.", "start": 0, "end": 7}],
        raise_on_verify=True,
    )
    result = _mapper(llm).map_claims(
        "A fact.", [_hit("c1", "doc-1", "something")], "trace-1"
    )
    item = result.claims[0].evidence_items[0]
    assert item.verification_result == VerificationResult.undetermined
    assert item.coverage == EvidenceCoverage.none
    # Every item undetermined => verification_unavailable (R1.8).
    assert result.claims[0].evidence_status == EvidenceStatus.verification_unavailable


def test_verification_unparseable_degrades_to_undetermined():
    llm = _StubLLM(
        decomposition=[{"text": "A fact.", "start": 0, "end": 7}],
        verify_returns_garbage=True,
    )
    result = _mapper(llm).map_claims(
        "A fact.", [_hit("c1", "doc-1", "something")], "trace-1"
    )
    item = result.claims[0].evidence_items[0]
    assert item.verification_result == VerificationResult.undetermined
    assert item.coverage == EvidenceCoverage.none


# ---------------------------------------------------------------------------
# Conflicting evidence detection (R3.4)
# ---------------------------------------------------------------------------


def test_conflicting_evidence_flagged_across_different_documents():
    llm = _StubLLM(
        decomposition=[{"text": "Revenue was 10.", "start": 0, "end": 15}],
        verifications={
            ("Revenue was 10.", "alpha"): ("entails", "full"),
            ("Revenue was 10.", "beta"): ("does_not_entail", "none"),
        },
    )
    result = _mapper(llm).map_claims(
        "Revenue was 10.",
        [
            _hit("c1", "doc-1", "alpha passage"),
            _hit("c2", "doc-2", "beta passage"),
        ],
        "trace-1",
    )
    claim = result.claims[0]
    assert claim.claim_id in result.conflicting_claim_ids
    assert result.conflicting_claim_ids == {claim.claim_id}


def test_no_conflict_when_entails_and_refutes_share_document():
    llm = _StubLLM(
        decomposition=[{"text": "Revenue was 10.", "start": 0, "end": 15}],
        verifications={
            ("Revenue was 10.", "alpha"): ("entails", "full"),
            ("Revenue was 10.", "beta"): ("does_not_entail", "none"),
        },
    )
    result = _mapper(llm).map_claims(
        "Revenue was 10.",
        [
            _hit("c1", "doc-1", "alpha passage"),
            _hit("c2", "doc-1", "beta passage"),
        ],
        "trace-1",
    )
    # Both items come from doc-1, so there is no cross-document contradiction.
    assert result.conflicting_claim_ids == set()


# ---------------------------------------------------------------------------
# Concurrent verification: order preservation and claim cap
# ---------------------------------------------------------------------------


def test_evidence_order_preserved_under_concurrent_verification():
    """Each claim's evidence items stay in hit order regardless of the order in
    which the concurrent per-pair verification calls complete."""
    from rag_system.claims import MAX_CLAIMS

    assert MAX_CLAIMS >= 1  # sanity: cap constant is exposed

    llm = _StubLLM(
        decomposition=[{"text": "Revenue was 10.", "start": 0, "end": 15}],
        verifications={
            ("Revenue was 10.", "alpha"): ("entails", "full"),
            ("Revenue was 10.", "beta"): ("does_not_entail", "none"),
            ("Revenue was 10.", "gamma"): ("undetermined", "none"),
        },
    )
    mapper = _mapper(llm)
    result = mapper.map_claims(
        "Revenue was 10.",
        [
            _hit("c-alpha", "doc-a", "alpha"),
            _hit("c-beta", "doc-b", "beta"),
            _hit("c-gamma", "doc-c", "gamma"),
        ],
        "trace-order",
    )

    items = result.claims[0].evidence_items
    # Evidence is reassembled in stable hit order (alpha, beta, gamma).
    assert [item.quote for item in items] == ["alpha", "beta", "gamma"]
    assert items[0].verification_result == VerificationResult.entails
    assert items[1].verification_result == VerificationResult.does_not_entail
    assert items[2].verification_result == VerificationResult.undetermined


def test_claim_count_is_capped():
    """Decomposition yielding more than MAX_CLAIMS claims is capped."""
    from rag_system.claims import MAX_CLAIMS

    decomposition = [
        {"text": f"Fact {i}.", "start": 0, "end": 0} for i in range(MAX_CLAIMS + 25)
    ]
    llm = _StubLLM(decomposition=decomposition)
    result = _mapper(llm).map_claims(
        "irrelevant answer text",
        [_hit("c1", "doc-a", "quote")],
        "trace-cap",
    )
    assert len(result.claims) == MAX_CLAIMS
