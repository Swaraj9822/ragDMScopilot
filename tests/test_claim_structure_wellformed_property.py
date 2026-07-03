# Feature: rag-trust-and-observability, Property 1: Claim structure is well-formed
"""Property-based test for claim structure well-formedness (task 2.4).

Feature: rag-trust-and-observability.

**Property 1: Claim structure is well-formed.**

**Validates: Requirements 1.1, 1.14.**

*For any* answer text and any (varied) LLM decomposition — including
out-of-bounds, negative, and inverted offsets — :meth:`ClaimMapper.map_claims`
produces claims whose structure is always well-formed:

* every ``answer_span`` satisfies ``0 <= start <= end <= len(answer)`` (R1.1),
  because the mapper clamps model-provided offsets to the answer bounds;
* every claim carries exactly one ``EvidenceStatus`` from the defined set
  (R1.14);
* ``claim_id`` is stable across re-reads of the same ``trace_id`` — mapping the
  same answer twice yields identical ids in order (R1.1).

Model outputs are stubbed for determinism (see ``tests/test_claim_mapper.py``
for the stub pattern): the fake LLM routes on prompt content to return a canned
decomposition for the decomposition call and a canned verdict for each
entailment call, so decomposition, span clamping, and status derivation are
exercised across many generated inputs without any live model call.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.claims import ClaimMapper, derive_claim_id
from rag_system.models import (
    Chunk,
    EvidenceCoverage,
    EvidenceStatus,
    RetrievalHit,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Test double (stubbed LLM, mirrors tests/test_claim_mapper.py)
# ---------------------------------------------------------------------------


class _StubLLM:
    """Fake TextLLM: canned decomposition + a single canned entailment verdict.

    Args:
        decomposition: The list of ``{"text", "start", "end"}`` dicts the
            decomposition call returns (a well-formed, non-``None`` list, so the
            mapper never takes the R1.9 failure path).
        verification: ``(verification_result, coverage)`` string pair returned
            for every entailment (per-pair) call, letting the derived
            ``evidence_status`` range over the whole enum across examples while
            staying deterministic across re-reads.
    """

    model_id = "stub-generator"
    provider = "stub"

    def __init__(self, decomposition, verification) -> None:
        self._decomposition = decomposition
        self._result, self._coverage = verification

    def generate(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        if "Decompose the following answer" in prompt:
            return json.dumps({"claims": self._decomposition}), {}
        return (
            json.dumps(
                {
                    "verification_result": self._result,
                    "coverage": self._coverage,
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


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Printable, non-template text so the stub can reliably distinguish the
# decomposition prompt from entailment prompts.
_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    max_size=40,
)

# A single factual statement; text is non-empty (blank statements are dropped by
# the parser). Offsets range freely, including negative, inverted, and
# out-of-bounds, to exercise the mapper's clamping.
_NON_BLANK = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=20,
)
_OFFSET = st.integers(min_value=-25, max_value=80)


@st.composite
def _decomposed_claim(draw: st.DrawFn) -> dict:
    return {
        "text": draw(_NON_BLANK),
        "start": draw(_OFFSET),
        "end": draw(_OFFSET),
    }


# 0..6 decomposed claims keeps the property fast while covering empty, single,
# and multi-claim answers.
_decomposition = st.lists(_decomposed_claim(), min_size=0, max_size=6)


@st.composite
def _hit(draw: st.DrawFn) -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(
            id=draw(st.text(alphabet="abcdef0123456789", min_size=1, max_size=6)),
            document_id=f"doc-{draw(st.integers(min_value=0, max_value=3))}",
            version="v1",
            text=draw(_SAFE_TEXT),
        ),
        score=draw(st.floats(min_value=0.0, max_value=1.0)),
        source="test",
    )


_hits = st.lists(_hit(), min_size=0, max_size=5)

_verification = st.tuples(
    st.sampled_from([v.value for v in VerificationResult]),
    st.sampled_from([c.value for c in EvidenceCoverage]),
)

_trace_id = st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12)


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------


@settings(max_examples=250)
@given(
    answer=_SAFE_TEXT,
    decomposition=_decomposition,
    hits=_hits,
    verification=_verification,
    trace_id=_trace_id,
)
def test_claim_structure_is_well_formed(
    answer: str,
    decomposition: list[dict],
    hits: list[RetrievalHit],
    verification: tuple[str, str],
    trace_id: str,
) -> None:
    result = _mapper(_StubLLM(decomposition, verification)).map_claims(
        answer, hits, trace_id
    )

    # A well-formed (non-``None``) decomposition is never a failure (R1.9 is a
    # separate path exercised in the unit tests).
    assert result.decomposition_failed is False

    for index, claim in enumerate(result.claims):
        # R1.1: offsets are clamped into the answer bounds.
        span = claim.answer_span
        assert 0 <= span.start <= span.end <= len(answer)

        # R1.14: exactly one status from the defined set per claim.
        assert isinstance(claim.evidence_status, EvidenceStatus)
        assert claim.evidence_status in set(EvidenceStatus)

        # R1.1: id is derived deterministically from (trace_id, claim_index).
        assert claim.claim_id == derive_claim_id(trace_id, index)


@settings(max_examples=150)
@given(
    answer=_SAFE_TEXT,
    decomposition=_decomposition,
    hits=_hits,
    verification=_verification,
    trace_id=_trace_id,
)
def test_claim_ids_are_stable_across_re_reads(
    answer: str,
    decomposition: list[dict],
    hits: list[RetrievalHit],
    verification: tuple[str, str],
    trace_id: str,
) -> None:
    # R1.1: re-reading the same (trace_id) answer yields identical ids in order.
    first = _mapper(_StubLLM(decomposition, verification)).map_claims(
        answer, hits, trace_id
    )
    second = _mapper(_StubLLM(decomposition, verification)).map_claims(
        answer, hits, trace_id
    )
    assert [c.claim_id for c in first.claims] == [c.claim_id for c in second.claims]
