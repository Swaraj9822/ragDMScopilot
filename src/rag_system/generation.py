from textwrap import dedent
from typing import Any

from rag_system.config import Settings
from rag_system.models import Citation, QueryResponse, RetrievalHit
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)


class BedrockNemotronGenerator:
    def __init__(self, settings: Settings):
        self._client = settings.boto3_session().client("bedrock-runtime")
        self._model_id = settings.bedrock_model_id

    @retry_on_transient()
    def _call_bedrock(self, prompt: str) -> tuple[str, dict[str, Any]]:
        """Call Bedrock converse API with retry on transient failures."""
        response = self._client.converse(
            modelId=self._model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": 0.1, "maxTokens": 4096},
        )
        return response["output"]["message"]["content"][0]["text"], response.get("usage", {})

    def answer(self, question: str, hits: list[RetrievalHit], trace_id: str) -> QueryResponse:
        log_extra = {"trace_id": trace_id, "hit_count": len(hits), "model_id": self._model_id}
        citations = [
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
            f"[{index}] document={hit.chunk.document_id} "
            f"pages={hit.chunk.page_start}-{hit.chunk.page_end}\n{hit.chunk.text}"
            for index, hit in enumerate(hits, start=1)
        )
        prompt = build_grounded_prompt(question, context)
        prompt_chars = len(prompt)
        context_chars = len(context)

        logger.info(
            "Calling Bedrock %s (prompt=%d chars, context=%d chars, %d citations)",
            self._model_id,
            prompt_chars,
            context_chars,
            len(citations),
            extra={
                **log_extra,
                "prompt_chars": prompt_chars,
                "context_chars": context_chars,
                "citation_count": len(citations),
            },
        )
        metrics.observe("rag_generation_prompt_chars", prompt_chars, {"model_id": self._model_id})
        metrics.observe("rag_generation_context_chars", context_chars, {"model_id": self._model_id})
        metrics.observe("rag_generation_citation_count", len(citations), {"model_id": self._model_id})

        answer_text, usage = self._call_bedrock(prompt)
        answer_chars = len(answer_text)
        for token_name, token_count in usage.items():
            if isinstance(token_count, (int, float)):
                metrics.observe(
                    "rag_generation_tokens",
                    float(token_count),
                    {"model_id": self._model_id, "token_type": token_name},
                )
        logger.info(
            "Bedrock returned %d-char answer (trace=%s, usage=%s)",
            answer_chars,
            trace_id,
            usage,
            extra={**log_extra, "answer_chars": answer_chars},
        )

        evidence_status = "grounded" if hits else "insufficient_evidence"
        return QueryResponse(
            answer=answer_text,
            citations=citations,
            evidence_status=evidence_status,
            trace_id=trace_id,
        )


def build_grounded_prompt(question: str, context: str) -> str:
    if not context.strip():
        context = "No retrieved evidence."
    return dedent(
        f"""
        You are answering questions over a private business PDF corpus.
        Use only the provided context. If the context is insufficient, say that the available documents do not contain enough evidence.
        Include concise page-aware citations in the answer when evidence is used.

        Question:
        {question}

        Context:
        {context}

        Return a direct answer. Do not invent facts outside the context.
        """
    ).strip()


