from rag_system.claims import ClaimMappingResult
from rag_system.generation import GroundedAnswerGenerator, build_grounded_prompt
from rag_system.models import Chunk, RetrievalHit


class _NoOpClaimMapper:
    """Claim mapper that returns no claims (used to keep existing tests working)."""

    def map_claims(self, answer_text, hits, trace_id):
        return ClaimMappingResult(claims=[], decomposition_failed=False, conflicting_claim_ids=set())


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


def test_generator_fails_closed_on_unparseable_output() -> None:
    # Malformed (non-JSON) output must NOT be credited with every retrieved
    # chunk and labelled grounded. It fails closed: prose kept, no citations,
    # evidence marked insufficient.
    generator = _generator_with_response("Revenue was probably around 10, I think.")

    response = generator.answer("What was revenue?", [_hit("chunk-1"), _hit("chunk-2")], "trace-3")

    assert response.citations == []
    assert response.evidence_status == "insufficient_evidence"
    assert response.confidence == "low"
    assert response.answer == "Revenue was probably around 10, I think."
    assert response.insufficient_evidence_reason


def test_generator_fails_closed_with_fallback_when_output_empty() -> None:
    generator = _generator_with_response("")

    response = generator.answer("What was revenue?", [_hit("chunk-1")], "trace-3b")

    assert response.citations == []
    assert response.evidence_status == "insufficient_evidence"
    assert response.answer == "The available documents do not contain enough evidence."


def test_answer_stream_fails_closed_without_metadata_block() -> None:
    # A stream that never emits the metadata block cannot be verified, so the
    # final response fails closed even though the prose was streamed.
    generator = _stream_generator(["Revenue ", "was 10."])

    events = list(generator.answer_stream("What was revenue?", [_hit("chunk-1")], "trace-4"))

    final = next(event for event in events if event["type"] == "final")["response"]
    assert final.citations == []
    assert final.evidence_status == "insufficient_evidence"
    assert final.confidence == "low"
    assert final.insufficient_evidence_reason
    deltas = "".join(event["text"] for event in events if event["type"] == "delta")
    assert "Revenue was 10." in deltas


def test_answer_stream_grounds_with_valid_metadata() -> None:
    # Guard against over-failing: a well-formed metadata block still grounds.
    meta = (
        '{"used_citation_ids": ["chunk-1"], "confidence": "high", '
        '"insufficient_evidence_reason": null}'
    )
    generator = _stream_generator(["Revenue was 10.\n", "###META###", meta])

    events = list(generator.answer_stream("What was revenue?", [_hit("chunk-1")], "trace-5"))

    final = next(event for event in events if event["type"] == "final")["response"]
    assert final.evidence_status == "grounded"
    assert final.confidence == "high"
    assert [citation.chunk_id for citation in final.citations] == ["chunk-1"]


def _generator_with_response(raw_text: str) -> GroundedAnswerGenerator:
    generator = object.__new__(GroundedAnswerGenerator)
    generator._model_id = "fake-generator"
    generator._call_llm = lambda prompt: (raw_text.strip(), {"inputTokens": 1})
    generator._claim_mapper = _NoOpClaimMapper()
    return generator


class _FakeStreamLLM:
    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces

    def generate_stream(self, prompt: str, temperature: float = 0.1, max_tokens: int = 4096):
        yield from self._pieces


def _stream_generator(pieces: list[str]) -> GroundedAnswerGenerator:
    generator = object.__new__(GroundedAnswerGenerator)
    generator._model_id = "fake-generator"
    generator._llm = _FakeStreamLLM(pieces)
    generator._claim_mapper = _NoOpClaimMapper()
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
