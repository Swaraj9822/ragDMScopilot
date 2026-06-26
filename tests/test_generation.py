from rag_system.generation import GroundedAnswerGenerator, build_grounded_prompt
from rag_system.models import Chunk, RetrievalHit


def test_prompt_requires_grounding() -> None:
    prompt = build_grounded_prompt("What is revenue?", "Revenue was 10 on page 2.")

    assert "Use only the provided context" in prompt
    assert "Return only JSON" in prompt
    assert "What is revenue?" in prompt
    assert "Revenue was 10" in prompt


def test_generator_uses_only_valid_structured_citations() -> None:
    generator = _generator_with_response(
        """
        {
          "answer": "Revenue was 10.",
          "used_citation_ids": ["chunk-1", "missing"],
          "confidence": "high",
          "insufficient_evidence_reason": null
        }
        """
    )

    response = generator.answer("What was revenue?", [_hit("chunk-1")], "trace-1")

    assert response.answer == "Revenue was 10."
    assert response.evidence_status == "grounded"
    assert response.confidence == "high"
    assert [citation.chunk_id for citation in response.citations] == ["chunk-1"]


def test_generator_marks_insufficient_when_model_uses_no_valid_citations() -> None:
    generator = _generator_with_response(
        """
        {
          "answer": "The available documents do not contain enough evidence.",
          "used_citation_ids": ["missing"],
          "confidence": "low",
          "insufficient_evidence_reason": "The retrieved chunk does not answer the question."
        }
        """
    )

    response = generator.answer("What was margin?", [_hit("chunk-1")], "trace-2")

    assert response.evidence_status == "insufficient_evidence"
    assert response.confidence == "low"
    assert response.citations == []
    assert "does not answer" in response.insufficient_evidence_reason


def _generator_with_response(raw_text: str) -> GroundedAnswerGenerator:
    generator = object.__new__(GroundedAnswerGenerator)
    generator._model_id = "fake-generator"
    generator._call_llm = lambda prompt: (raw_text.strip(), {"inputTokens": 1})
    return generator


def _hit(chunk_id: str) -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(
            id=chunk_id,
            document_id="doc-1",
            version="v1",
            text="Revenue was 10 on page 2.",
            page_start=2,
            page_end=2,
            metadata={"source_filename": "report.pdf"},
        ),
        score=0.99,
        source="test",
    )
