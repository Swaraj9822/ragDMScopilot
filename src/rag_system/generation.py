import json
import re
from textwrap import dedent
from typing import Any, Iterator

from rag_system.claims import ClaimMapper, ClaimMappingResult
from rag_system.config import Settings
from rag_system.confidence import rag_confidence_score
from rag_system.llm import build_text_llm
from rag_system.models import Citation, QueryResponse, RetrievalHit
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)

#: Marker separating the streamed prose answer from the trailing JSON metadata
#: in the streaming grounded-answer contract.
META_MARKER = "###META###"


class GroundedAnswerGenerator:
    """RAG answer generator backed by the Gemini text LLM."""

    def __init__(self, settings: Settings):
        self._llm = build_text_llm(settings)
        self._model_id = self._llm.model_id
        self._claim_mapper = ClaimMapper(settings, llm=self._llm)

    @retry_on_transient()
    def _call_llm(self, prompt: str) -> tuple[str, dict[str, Any]]:
        """Call the configured LLM with retry on transient failures."""
        return self._llm.generate(prompt, temperature=0.1, max_tokens=4096)

    def answer(self, question: str, hits: list[RetrievalHit], trace_id: str) -> QueryResponse:
        log_extra = {"trace_id": trace_id, "hit_count": len(hits), "model_id": self._model_id}
        candidate_citations = [
            Citation(
                document_id=hit.chunk.document_id,
                chunk_id=hit.chunk.id,
                page_start=hit.chunk.page_start,
                page_end=hit.chunk.page_end,
                title=hit.chunk.metadata.get("source_filename"),
            )
            for hit in hits
        ]
        context = "\n\n".join(
            f"[{index}] chunk_id={hit.chunk.id} document={hit.chunk.document_id} "
            f"pages={hit.chunk.page_start}-{hit.chunk.page_end}\n{hit.chunk.text}"
            for index, hit in enumerate(hits, start=1)
        )
        prompt = build_grounded_prompt(question, context)
        prompt_chars = len(prompt)
        context_chars = len(context)

        logger.info(
            "Calling LLM %s (prompt=%d chars, context=%d chars, %d citations)",
            self._model_id,
            prompt_chars,
            context_chars,
            len(candidate_citations),
            extra={
                **log_extra,
                "prompt_chars": prompt_chars,
                "context_chars": context_chars,
                "citation_count": len(candidate_citations),
            },
        )
        metrics.observe("rag_generation_prompt_chars", prompt_chars, {"model_id": self._model_id})
        metrics.observe("rag_generation_context_chars", context_chars, {"model_id": self._model_id})
        metrics.observe(
            "rag_generation_citation_count",
            len(candidate_citations),
            {"model_id": self._model_id},
        )

        raw_answer, usage = self._call_llm(prompt)
        answer_chars = len(raw_answer)
        for token_name, token_count in usage.items():
            if isinstance(token_count, (int, float)):
                metrics.observe(
                    "rag_generation_tokens",
                    float(token_count),
                    {"model_id": self._model_id, "token_type": token_name},
                )
        logger.info(
            "LLM returned %d-char answer (trace=%s, usage=%s)",
            answer_chars,
            trace_id,
            usage,
            extra={**log_extra, "answer_chars": answer_chars},
        )

        parsed = _parse_structured_answer(raw_answer)
        if parsed is None:
            # Fail closed. The model did not return the required JSON, so we
            # cannot verify which chunks (if any) actually support the prose.
            # Crediting every retrieved chunk here (the previous behaviour) let
            # unverifiable prose be labelled "grounded". Keep the prose but
            # attach no citations and mark the evidence insufficient.
            metrics.increment(
                "rag_generation_unparseable_output_total", {"model_id": self._model_id}
            )
            logger.warning(
                "LLM answer was not valid structured output; failing closed with no citations",
                extra=log_extra,
            )
            answer_text = (
                raw_answer.strip()
                or "The available documents do not contain enough evidence."
            )
            used_chunk_ids = []
            confidence = "low"
            insufficient_reason = (
                "Model response could not be validated against the required schema, "
                "so no citations were verified."
            )
        else:
            answer_text = str(parsed.get("answer") or "").strip()
            if not answer_text:
                answer_text = "The available documents do not contain enough evidence."
            used_chunk_ids = _extract_used_citation_ids(parsed)
            confidence = _normalise_confidence(parsed.get("confidence"))
            reason = parsed.get("insufficient_evidence_reason")
            insufficient_reason = str(reason).strip() if reason else None

        citations = _validate_citations(candidate_citations, used_chunk_ids)
        invalid_citation_count = max(0, len(set(used_chunk_ids)) - len(citations))
        if invalid_citation_count:
            metrics.observe(
                "rag_generation_invalid_citation_count",
                invalid_citation_count,
                {"model_id": self._model_id},
            )
            logger.warning(
                "Generated answer referenced %d invalid citation id(s)",
                invalid_citation_count,
                extra={**log_extra, "citation_count": len(citations)},
            )

        evidence_status = _evidence_status(hits, citations, insufficient_reason)
        confidence_score = rag_confidence_score(
            evidence_status=evidence_status,
            model_confidence=confidence,
            retrieval_scores=[hit.score for hit in hits],
            citation_count=len(citations),
            hit_count=len(hits),
            has_insufficient_reason=bool(insufficient_reason),
            avg_logprob=usage.get("avgLogprob") if isinstance(usage, dict) else None,
        )
        metrics.observe(
            "rag_generation_confidence_score",
            confidence_score,
            {"model_id": self._model_id, "evidence_status": evidence_status},
        )

        # --- Claim-level evidence mapping (R1, task 2.6) ---
        # Call the mapper after prose + citation validation; on decomposition
        # failure return empty claims list with claim_decomposition_failed=true
        # without raising.
        claim_result = self._run_claim_mapping(answer_text, hits, trace_id)

        return QueryResponse(
            answer=answer_text,
            citations=citations,
            evidence_status=evidence_status,
            trace_id=trace_id,
            confidence=confidence,
            confidence_score=confidence_score,
            insufficient_evidence_reason=insufficient_reason,
            claims=claim_result.claims,
            claim_decomposition_failed=claim_result.decomposition_failed,
            retrieval_scores=[hit.score for hit in hits],
        )

    def answer_stream(
        self, question: str, hits: list[RetrievalHit], trace_id: str
    ) -> Iterator[dict[str, Any]]:
        """Stream a grounded answer.

        Yields ``{"type": "delta", "text": ...}`` events as the prose answer is
        produced, then a single ``{"type": "final", "response": QueryResponse}``
        carrying citations, confidence, and the numeric confidence score.

        The model is prompted to emit the answer as prose followed by a
        ``META_MARKER`` line and a small JSON metadata object. We stream the
        prose and parse the trailing metadata once the stream completes — a
        single LLM call that preserves the existing grounding/citation logic.
        """
        log_extra = {"trace_id": trace_id, "hit_count": len(hits), "model_id": self._model_id}
        candidate_citations = [
            Citation(
                document_id=hit.chunk.document_id,
                chunk_id=hit.chunk.id,
                page_start=hit.chunk.page_start,
                page_end=hit.chunk.page_end,
                title=hit.chunk.metadata.get("source_filename"),
            )
            for hit in hits
        ]
        context = "\n\n".join(
            f"[{index}] chunk_id={hit.chunk.id} document={hit.chunk.document_id} "
            f"pages={hit.chunk.page_start}-{hit.chunk.page_end}\n{hit.chunk.text}"
            for index, hit in enumerate(hits, start=1)
        )
        prompt = build_grounded_stream_prompt(question, context)
        logger.info(
            "Streaming LLM %s (context=%d chars, %d citations)",
            self._model_id,
            len(context),
            len(candidate_citations),
            extra=log_extra,
        )

        buffer = ""
        emitted = 0
        marker_len = len(META_MARKER)
        for piece in self._llm.generate_stream(prompt, temperature=0.1, max_tokens=4096):
            buffer += piece
            idx = buffer.find(META_MARKER)
            if idx != -1:
                # Emit any answer text up to the marker, then stop forwarding —
                # everything after the marker is metadata, not answer prose.
                if idx > emitted:
                    yield {"type": "delta", "text": buffer[emitted:idx]}
                    emitted = idx
            else:
                # Hold back the last (marker_len - 1) chars so a marker split
                # across chunk boundaries is never emitted as answer text.
                safe = len(buffer) - (marker_len - 1)
                if safe > emitted:
                    yield {"type": "delta", "text": buffer[emitted:safe]}
                    emitted = safe

        marker_idx = buffer.find(META_MARKER)
        if marker_idx != -1:
            answer_text = buffer[:marker_idx].strip()
            meta_raw = buffer[marker_idx + marker_len :].strip()
        else:
            # No marker — flush whatever prose we were holding back and treat
            # the whole buffer as the answer with no structured metadata.
            if len(buffer) > emitted:
                yield {"type": "delta", "text": buffer[emitted:]}
            answer_text = buffer.strip()
            meta_raw = ""

        parsed = _parse_structured_answer(meta_raw) if meta_raw else None
        if parsed is None:
            # Fail closed: the trailing metadata block was missing or malformed,
            # so no citations can be verified. Attach none and mark evidence
            # insufficient rather than crediting every retrieved chunk. (The
            # prose was already streamed to the user as deltas above.)
            metrics.increment(
                "rag_generation_unparseable_output_total", {"model_id": self._model_id}
            )
            logger.warning(
                "Streamed answer had no valid metadata block; failing closed with no citations",
                extra=log_extra,
            )
            used_chunk_ids = []
            confidence = "low"
            insufficient_reason = (
                "Model response could not be validated against the required schema, "
                "so no citations were verified."
            )
        else:
            used_chunk_ids = _extract_used_citation_ids(parsed)
            confidence = _normalise_confidence(parsed.get("confidence"))
            reason = parsed.get("insufficient_evidence_reason")
            insufficient_reason = str(reason).strip() if reason else None

        if not answer_text:
            answer_text = "The available documents do not contain enough evidence."

        citations = _validate_citations(candidate_citations, used_chunk_ids)
        evidence_status = _evidence_status(hits, citations, insufficient_reason)
        confidence_score = rag_confidence_score(
            evidence_status=evidence_status,
            model_confidence=confidence,
            retrieval_scores=[hit.score for hit in hits],
            citation_count=len(citations),
            hit_count=len(hits),
            has_insufficient_reason=bool(insufficient_reason),
        )
        metrics.observe(
            "rag_generation_confidence_score",
            confidence_score,
            {"model_id": self._model_id, "evidence_status": evidence_status},
        )

        # --- Claim-level evidence mapping (R1, task 2.6) ---
        claim_result = self._run_claim_mapping(answer_text, hits, trace_id)

        yield {
            "type": "final",
            "response": QueryResponse(
                answer=answer_text,
                citations=citations,
                evidence_status=evidence_status,
                trace_id=trace_id,
                confidence=confidence,
                confidence_score=confidence_score,
                insufficient_evidence_reason=insufficient_reason,
                claims=claim_result.claims,
                claim_decomposition_failed=claim_result.decomposition_failed,
                retrieval_scores=[hit.score for hit in hits],
            ),
        }

    def _run_claim_mapping(
        self, answer_text: str, hits: list[RetrievalHit], trace_id: str
    ) -> ClaimMappingResult:
        """Run claim decomposition + evidence verification (R1, task 2.6).

        Called after prose generation and citation validation. On any failure
        returns an empty claims list with ``decomposition_failed=True`` without
        raising (R1.9).
        """
        try:
            return self._claim_mapper.map_claims(answer_text, hits, trace_id)
        except Exception:  # noqa: BLE001 - fail closed, never raise (R1.9)
            logger.warning(
                "Claim mapping raised unexpectedly; returning empty claims",
                extra={"trace_id": trace_id, "model_id": self._model_id},
            )
            return ClaimMappingResult(
                claims=[], decomposition_failed=True, conflicting_claim_ids=set()
            )


def build_grounded_prompt(question: str, context: str) -> str:
    if not context.strip():
        context = "No retrieved evidence."
    return dedent(
        f"""
        You are answering questions over a private business PDF corpus.
        Use only the provided context. If the context is insufficient, say that the available documents do not contain enough evidence.
        Cite evidence only by using chunk_id values that appear in the context.

        Question:
        {question}

        Context:
        {context}

        Return only JSON with this exact shape:
        {{
          "answer": "direct answer grounded in the context",
          "used_citation_ids": ["chunk_id from context"],
          "confidence": "high|medium|low",
          "insufficient_evidence_reason": null
        }}

        If the context is insufficient, use an empty used_citation_ids list, set confidence to "low", and explain why in insufficient_evidence_reason.
        Do not invent facts outside the context.
        """
    ).strip()


def build_grounded_stream_prompt(question: str, context: str) -> str:
    """Prompt for the streaming contract: prose answer, then a metadata tail.

    The answer is streamed to the user as plain prose; the trailing
    ``META_MARKER`` + JSON block carries citations/confidence and is parsed
    server-side once the stream completes (it is never shown to the user).
    """
    if not context.strip():
        context = "No retrieved evidence."
    return dedent(
        f"""
        You are answering questions over a private business PDF corpus.
        Use only the provided context. If the context is insufficient, say that the available documents do not contain enough evidence.

        First, write the answer for the user in clear plain prose. Do not use JSON or any preamble for the answer.
        After the complete answer, output a single line containing exactly:
        {META_MARKER}
        and immediately after it a single-line JSON object with this exact shape:
        {{"used_citation_ids": ["chunk_id from context"], "confidence": "high|medium|low", "insufficient_evidence_reason": null}}

        Cite evidence only by using chunk_id values that appear in the context.
        If the context is insufficient, write that in the prose answer, then set used_citation_ids to an empty list, confidence to "low", and explain why in insufficient_evidence_reason.
        Do not invent facts outside the context.

        Question:
        {question}

        Context:
        {context}
        """
    ).strip()


def _parse_structured_answer(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_used_citation_ids(payload: dict[str, Any]) -> list[str]:
    raw_ids = payload.get("used_citation_ids", payload.get("used_citations", []))
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids]
    citation_ids = []
    for raw_id in raw_ids:
        citation_id = str(raw_id).strip()
        if citation_id:
            citation_ids.append(citation_id)
    return citation_ids


def _normalise_confidence(value: Any) -> str:
    confidence = str(value or "medium").strip().lower()
    return confidence if confidence in {"high", "medium", "low"} else "medium"


def _validate_citations(
    candidates: list[Citation],
    used_chunk_ids: list[str],
) -> list[Citation]:
    by_chunk_id = {citation.chunk_id: citation for citation in candidates}
    citations: list[Citation] = []
    seen: set[str] = set()
    for chunk_id in used_chunk_ids:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        citation = by_chunk_id.get(chunk_id)
        if citation is not None:
            citations.append(citation)
    return citations


def _evidence_status(
    hits: list[RetrievalHit],
    citations: list[Citation],
    insufficient_reason: str | None,
) -> str:
    if not hits:
        return "insufficient_evidence"
    if citations and not insufficient_reason:
        return "grounded"
    if citations:
        return "partially_grounded"
    return "insufficient_evidence"
