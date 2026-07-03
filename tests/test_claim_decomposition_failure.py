"""Unit tests for claim decomposition failure path (task 2.7, R1.9).

When claim decomposition fails (model error/timeout/unparseable output), the
generation should return the answer with an empty claims list and
claim_decomposition_failed=True, never raising an exception.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from rag_system.claims import ClaimMapper
from rag_system.generation import GroundedAnswerGenerator
from rag_system.models import Chunk, RetrievalHit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FailingLLM:
    """LLM stub that raises on every generate call (simulates model error/timeout)."""

    model_id: str = "failing-llm"
    provider: str = "fake"

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking_budget: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        raise RuntimeError("Simulated model timeout/error")

    def generate_stream(
        self, prompt: str, *, temperature: float = 0.0, max_tokens: int = 4096, thinking_budget: int | None = None
    ) -> Iterator[str]:
        raise RuntimeError("Simulated model timeout/error")


class _UnparseableLLM:
    """LLM stub that returns unparseable output for decomposition."""

    model_id: str = "unparseable-llm"
    provider: str = "fake"
    _call_count: int = 0

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking_budget: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        self._call_count += 1
        # First call is decomposition - return garbage that can't be parsed
        if self._call_count == 1:
            return ("not json at all {{{{ broken garbage", {"inputTokens": 5})
        # Subsequent calls (verification) shouldn't be reached but just in case
        return ("still garbage", {"inputTokens": 1})

    def generate_stream(
        self, prompt: str, *, temperature: float = 0.0, max_tokens: int = 4096, thinking_budget: int | None = None
    ) -> Iterator[str]:
        yield "not parseable"


def _make_generator_with_claim_mapper_llm(claim_llm) -> GroundedAnswerGenerator:
    """Create a GroundedAnswerGenerator where the generation LLM returns valid
    structured output but the ClaimMapper's LLM fails."""
    generator = object.__new__(GroundedAnswerGenerator)
    generator._model_id = "fake-generator"
    # The generation LLM returns valid structured JSON
    generator._call_llm = lambda prompt: (
        '{"answer": "Revenue was $10M in Q3.", "used_citation_ids": ["chunk-1"], '
        '"confidence": "high", "insufficient_evidence_reason": null}',
        {"inputTokens": 10},
    )
    # Use a ClaimMapper with the specified failing LLM
    generator._claim_mapper = ClaimMapper.__new__(ClaimMapper)
    generator._claim_mapper._llm = claim_llm
    generator._claim_mapper._model_id = claim_llm.model_id
    return generator


def _hit(chunk_id: str = "chunk-1") -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(
            id=chunk_id,
            document_id="doc-1",
            version="v1",
            text="Revenue was $10M in Q3 2024.",
            page_start=1,
            page_end=1,
            metadata={"source_filename": "financials.pdf"},
        ),
        score=0.95,
        source="test",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClaimDecompositionFailureModelError:
    """When the LLM raises an exception during decomposition (R1.9)."""

    def test_returns_answer_with_empty_claims_on_model_error(self) -> None:
        """Model error during decomposition returns the answer with empty claims."""
        generator = _make_generator_with_claim_mapper_llm(_FailingLLM())

        # Must not raise
        response = generator.answer("What was revenue?", [_hit()], "trace-fail-1")

        assert response.answer == "Revenue was $10M in Q3."
        assert response.claims == []
        assert response.claim_decomposition_failed is True

    def test_no_exception_raised_on_model_error(self) -> None:
        """Model error never propagates as an exception (R1.9)."""
        generator = _make_generator_with_claim_mapper_llm(_FailingLLM())

        # Explicitly assert no exception via pytest pattern
        try:
            response = generator.answer("What was revenue?", [_hit()], "trace-fail-2")
        except Exception as exc:
            pytest.fail(f"Expected no exception but got: {exc!r}")

        assert response is not None
        assert response.claim_decomposition_failed is True

    def test_citations_still_populated_on_model_error(self) -> None:
        """Citation validation still works even when claim mapping fails."""
        generator = _make_generator_with_claim_mapper_llm(_FailingLLM())

        response = generator.answer("What was revenue?", [_hit()], "trace-fail-3")

        # The generation LLM returned valid structured output with citations
        assert len(response.citations) == 1
        assert response.citations[0].chunk_id == "chunk-1"


class TestClaimDecompositionFailureUnparseableOutput:
    """When the LLM returns unparseable output during decomposition (R1.9)."""

    def test_returns_answer_with_empty_claims_on_unparseable_output(self) -> None:
        """Unparseable decomposition output returns the answer with empty claims."""
        generator = _make_generator_with_claim_mapper_llm(_UnparseableLLM())

        response = generator.answer("What was revenue?", [_hit()], "trace-unparse-1")

        assert response.answer == "Revenue was $10M in Q3."
        assert response.claims == []
        assert response.claim_decomposition_failed is True

    def test_no_exception_raised_on_unparseable_output(self) -> None:
        """Unparseable output never propagates as an exception (R1.9)."""
        generator = _make_generator_with_claim_mapper_llm(_UnparseableLLM())

        try:
            response = generator.answer("What was revenue?", [_hit()], "trace-unparse-2")
        except Exception as exc:
            pytest.fail(f"Expected no exception but got: {exc!r}")

        assert response is not None
        assert response.claim_decomposition_failed is True

    def test_answer_text_preserved_on_unparseable_output(self) -> None:
        """The answer text is correctly preserved despite decomposition failure."""
        generator = _make_generator_with_claim_mapper_llm(_UnparseableLLM())

        response = generator.answer("What was revenue?", [_hit()], "trace-unparse-3")

        assert response.answer == "Revenue was $10M in Q3."
        assert response.evidence_status in ("grounded", "partially_grounded", "insufficient_evidence")
        assert response.trace_id == "trace-unparse-3"
