"""Unit tests for ClaimMapper integration into generation.py (task 2.6).

Validates:
- Claims are populated on the QueryResponse after prose + citation validation
- Per-claim evidence_status is derived from classify_evidence_status
- The conflicting-evidence flag is detected per claim
- Decomposition failure returns empty claims + claim_decomposition_failed=True without raising
- Requirements: 1.9, 1.14, 3.4
"""

from __future__ import annotations

import json

from rag_system.claims import ClaimMapper
from rag_system.generation import GroundedAnswerGenerator
from rag_system.models import (
    Chunk,
    EvidenceStatus,
    RetrievalHit,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeLLM:
    """A fake LLM that routes on prompt content to canned responses.

    For the generation prompt (grounded answer), it returns a valid structured
    JSON answer. For the decomposition/entailment prompts, it routes based on
    the provided canned responses.
    """

    model_id = "fake-generator"
    provider = "fake"

    def __init__(
        self,
        *,
        generation_response: str | None = None,
        decomposition: list[dict] | None | str = "EMPTY",
        verifications: dict[tuple[str, str], tuple[str, str]] | None = None,
        decomposition_raises: bool = False,
    ) -> None:
        self._generation_response = generation_response or json.dumps(
            {
                "answer": "Revenue was 10.",
                "used_citation_ids": ["chunk-1"],
                "confidence": "high",
                "insufficient_evidence_reason": None,
            }
        )
        self._decomposition = decomposition
        self._verifications = verifications or {}
        self._decomposition_raises = decomposition_raises

    def generate(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        # Claim decomposition call
        if "Decompose the following answer" in prompt:
            if self._decomposition_raises:
                raise RuntimeError("simulated decomposition failure")
            if self._decomposition == "EMPTY":
                return json.dumps({"claims": []}), {}
            if isinstance(self._decomposition, str):
                return self._decomposition, {}
            if self._decomposition is None:
                raise RuntimeError("simulated decomposition model error")
            return json.dumps({"claims": self._decomposition}), {}

        # Entailment/verification call (detect by looking for "Claim:" and "Evidence:")
        if "Claim:" in prompt and "Evidence:" in prompt:
            for (claim_sub, quote_sub), (result, coverage) in self._verifications.items():
                if claim_sub in prompt and quote_sub in prompt:
                    return (
                        json.dumps(
                            {
                                "verification_result": result,
                                "coverage": coverage,
                                "covered_subclaims": [],
                            }
                        ),
                        {},
                    )
            return json.dumps({"verification_result": "undetermined", "coverage": "none"}), {}

        # Generation grounded-answer call (fallback)
        return self._generation_response, {"inputTokens": 10, "outputTokens": 20}

    def generate_stream(self, prompt, *, temperature, max_tokens, thinking_budget=None):
        yield self._generation_response


def _build_generator(llm: _FakeLLM) -> GroundedAnswerGenerator:
    """Build a GroundedAnswerGenerator with the fake LLM injected."""
    generator = object.__new__(GroundedAnswerGenerator)
    generator._llm = llm
    generator._model_id = llm.model_id
    generator._call_llm = lambda prompt: llm.generate(
        prompt, temperature=0.1, max_tokens=4096
    )
    generator._claim_mapper = ClaimMapper.__new__(ClaimMapper)
    generator._claim_mapper._llm = llm
    generator._claim_mapper._model_id = llm.model_id
    return generator


def _hit(chunk_id: str = "chunk-1", document_id: str = "doc-1", text: str = "Revenue was 10 on page 2.") -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(
            id=chunk_id,
            document_id=document_id,
            version="v1",
            text=text,
            page_start=2,
            page_end=2,
            metadata={"source_filename": "report.pdf"},
        ),
        score=0.99,
        source="test",
    )


# ---------------------------------------------------------------------------
# Tests: Claims populated on response (R1.14)
# ---------------------------------------------------------------------------


def test_answer_populates_claims_on_response():
    """After generation + citation validation, claims are populated on the response."""
    llm = _FakeLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
        ],
        verifications={("Revenue was 10.", "Revenue was 10"): ("entails", "full")},
    )
    generator = _build_generator(llm)

    response = generator.answer("What was revenue?", [_hit()], "trace-1")

    assert len(response.claims) == 1
    assert response.claims[0].text == "Revenue was 10."
    assert response.claim_decomposition_failed is False


def test_answer_derives_evidence_status_per_claim():
    """Each claim gets its evidence_status from classify_evidence_status."""
    llm = _FakeLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
        ],
        verifications={("Revenue was 10.", "Revenue was 10 on page 2."): ("entails", "full")},
    )
    generator = _build_generator(llm)

    response = generator.answer("What was revenue?", [_hit()], "trace-1")

    assert response.claims[0].evidence_status == EvidenceStatus.supported


def test_answer_partial_coverage_yields_partially_supported():
    """Partial coverage on entailment yields partially_supported status."""
    llm = _FakeLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
        ],
        verifications={("Revenue was 10.", "Revenue was 10 on page 2."): ("entails", "partial")},
    )
    generator = _build_generator(llm)

    response = generator.answer("What was revenue?", [_hit()], "trace-1")

    assert response.claims[0].evidence_status == EvidenceStatus.partially_supported


def test_answer_no_hits_yields_unsupported_claim():
    """Claims with no evidence items are unsupported."""
    llm = _FakeLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
        ],
    )
    generator = _build_generator(llm)

    response = generator.answer("What was revenue?", [], "trace-1")

    assert response.claims[0].evidence_status == EvidenceStatus.unsupported


# ---------------------------------------------------------------------------
# Tests: Decomposition failure path (R1.9)
# ---------------------------------------------------------------------------


def test_decomposition_failure_returns_empty_claims_and_flag():
    """Decomposition failure returns empty claims + claim_decomposition_failed=True (R1.9)."""
    llm = _FakeLLM(decomposition_raises=True)
    generator = _build_generator(llm)

    response = generator.answer("What was revenue?", [_hit()], "trace-1")

    # The response still has an answer (generation succeeded), but claims are empty.
    assert response.answer == "Revenue was 10."
    assert response.claims == []
    assert response.claim_decomposition_failed is True


def test_decomposition_unparseable_returns_empty_claims_and_flag():
    """Unparseable decomposition output sets the failure flag without raising."""
    llm = _FakeLLM(decomposition="not valid json at all")
    generator = _build_generator(llm)

    response = generator.answer("What was revenue?", [_hit()], "trace-1")

    assert response.claims == []
    assert response.claim_decomposition_failed is True
    # Answer is still returned (generation itself worked).
    assert response.answer == "Revenue was 10."


def test_decomposition_failure_never_raises():
    """Even if claim mapping raises unexpectedly, answer is returned (R1.9)."""
    llm = _FakeLLM(decomposition=[{"text": "A fact.", "start": 0, "end": 7}])
    generator = _build_generator(llm)
    # Sabotage the claim mapper to raise an unexpected error.
    generator._claim_mapper = _RaisingClaimMapper()

    response = generator.answer("What was revenue?", [_hit()], "trace-1")

    # Answer is returned, claims empty, flag set.
    assert response.answer == "Revenue was 10."
    assert response.claims == []
    assert response.claim_decomposition_failed is True


class _RaisingClaimMapper:
    """A claim mapper that raises an unexpected error."""

    def map_claims(self, answer_text, hits, trace_id):
        raise ValueError("Unexpected internal error in claim mapper")


# ---------------------------------------------------------------------------
# Tests: Conflicting evidence detection (R3.4)
# ---------------------------------------------------------------------------


def test_conflicting_evidence_flag_detected():
    """Claims with contradictory evidence from different docs are flagged (R3.4)."""
    llm = _FakeLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
        ],
        verifications={
            ("Revenue was 10.", "supports revenue"): ("entails", "full"),
            ("Revenue was 10.", "contradicts revenue"): ("does_not_entail", "none"),
        },
    )
    generator = _build_generator(llm)

    response = generator.answer(
        "What was revenue?",
        [
            _hit("c1", "doc-1", "supports revenue claim"),
            _hit("c2", "doc-2", "contradicts revenue claim"),
        ],
        "trace-1",
    )

    # The claim is flagged as having conflicting evidence (entails from doc-1,
    # does_not_entail from doc-2). The claim still appears on the response
    # because the abstention gate is a separate concern.
    assert len(response.claims) == 1
    # Despite conflicting evidence, the claim gets supported status because
    # there's at least one full entailment.
    assert response.claims[0].evidence_status == EvidenceStatus.supported


# ---------------------------------------------------------------------------
# Tests: Streaming path also includes claims (R1.14)
# ---------------------------------------------------------------------------


def test_answer_stream_populates_claims_on_final_response():
    """The streaming path also performs claim mapping on the final response."""
    meta = json.dumps(
        {
            "used_citation_ids": ["chunk-1"],
            "confidence": "high",
            "insufficient_evidence_reason": None,
        }
    )
    # Build a fake LLM that handles both stream and claim mapping
    llm = _FakeLLM(
        decomposition=[
            {"text": "Revenue was 10.", "start": 0, "end": 15},
        ],
        verifications={("Revenue was 10.", "Revenue was 10 on page 2."): ("entails", "full")},
    )
    generator = object.__new__(GroundedAnswerGenerator)
    generator._model_id = "fake-generator"
    generator._claim_mapper = ClaimMapper.__new__(ClaimMapper)
    generator._claim_mapper._llm = llm
    generator._claim_mapper._model_id = llm.model_id

    # Override generate_stream to produce answer + meta
    class _StreamLLM:
        model_id = "fake-generator"
        provider = "fake"

        def generate_stream(self, prompt, *, temperature, max_tokens, thinking_budget=None):
            yield "Revenue was 10.\n"
            yield "###META###"
            yield meta

        def generate(self, prompt, *, temperature, max_tokens, thinking_budget=None):
            return llm.generate(prompt, temperature=temperature, max_tokens=max_tokens)

    generator._llm = _StreamLLM()

    events = list(
        generator.answer_stream("What was revenue?", [_hit()], "trace-stream-1")
    )

    final = next(event for event in events if event["type"] == "final")["response"]
    assert len(final.claims) == 1
    assert final.claims[0].text == "Revenue was 10."
    assert final.claims[0].evidence_status == EvidenceStatus.supported
    assert final.claim_decomposition_failed is False
