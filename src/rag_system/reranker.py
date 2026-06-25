"""LLM-based reranker for scoring chunk relevance to a query.

Provides a RerankerProvider that uses Gemini 3.5 Flash on GCP Vertex AI
to score individual chunks with a pointwise relevance scoring prompt,
and an LLMReranker that orchestrates concurrent scoring with batching,
timeout, filtering, and fault isolation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait

from rag_system.config import Settings
from rag_system.models import RankedHit, RetrievalHit
from rag_system.observability import (
    CircuitOpenError,
    get_circuit_breaker,
    get_logger,
    get_trace_id,
    metrics,
    retry_on_transient,
    timed,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Transient error types for the reranker (mirrors GeminiProvider pattern)
# ---------------------------------------------------------------------------

try:
    from google.api_core.exceptions import (
        DeadlineExceeded,
        InternalServerError,
        ResourceExhausted,
        ServiceUnavailable,
        TooManyRequests,
    )

    _RERANKER_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
        ServiceUnavailable,
        TooManyRequests,
        ResourceExhausted,
        DeadlineExceeded,
        InternalServerError,
        ConnectionError,
        TimeoutError,
    )
except ImportError:
    _RERANKER_TRANSIENT_ERRORS = (ConnectionError, TimeoutError)

# ---------------------------------------------------------------------------
# Scoring prompt template
# ---------------------------------------------------------------------------

_SCORING_PROMPT_TEMPLATE = """\
You are a relevance scoring assistant. Rate how relevant the following text passage is to the given query.

Query: {query}

Passage: {chunk_text}

Respond with ONLY a decimal number between 0.0 and 1.0, where:
- 0.0 means completely irrelevant
- 1.0 means perfectly relevant

Score:"""


# ---------------------------------------------------------------------------
# RerankerProvider
# ---------------------------------------------------------------------------


class RerankerProvider:
    """Scores chunk relevance using Gemini on GCP Vertex AI.

    Handles Vertex AI SDK initialization, circuit breaker, retry logic, and
    response parsing for chunk scoring. Follows the same structural pattern as
    GeminiProvider but is purpose-built for single-score extraction.
    """

    name = "reranker"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model_id = settings.reranker_model_id
        self._project = settings.gcp_project_id
        self._location = settings.gcp_location
        self._cb_threshold = settings.circuit_failure_threshold
        self._cb_recovery = settings.circuit_recovery_timeout_s
        self._initialized = False  # lazy vertexai.init

    def _ensure_initialized(self) -> None:
        """Lazily import vertexai and initialize the SDK.

        Raises RuntimeError with actionable guidance if the library is missing
        or the required GCP_PROJECT_ID is not configured.
        """
        if self._initialized:
            return
        try:
            import vertexai  # noqa: F811
        except ImportError as e:
            raise RuntimeError(
                "google-cloud-aiplatform is required for the reranker provider. "
                "Install it with: pip install 'google-cloud-aiplatform>=1.60.0'"
            ) from e
        if not self._project:
            raise RuntimeError(
                "GCP_PROJECT_ID is required for the reranker. "
                "Set the GCP_PROJECT_ID environment variable."
            )
        vertexai.init(project=self._project, location=self._location)
        self._initialized = True

    def _build_model(self):
        """Construct a GenerativeModel, lazily initializing the SDK if needed."""
        self._ensure_initialized()
        from vertexai.generative_models import GenerativeModel

        return GenerativeModel(model_name=self._model_id)

    def score_chunk(self, query: str, chunk_text: str) -> float:
        """Score a chunk's relevance to the query using the LLM.

        Returns a float in [0.0, 1.0]. On circuit breaker open, raises
        CircuitOpenError. On non-parseable LLM responses, returns 0.0 and
        logs a warning.
        """
        cb = get_circuit_breaker("reranker", self._cb_threshold, self._cb_recovery)
        if not cb.allow_request():
            opened_ago = 0.0
            if cb._opened_at is not None:
                opened_ago = time.perf_counter() - cb._opened_at
            raise CircuitOpenError("reranker", opened_ago)
        try:
            score = self._score_inner(query, chunk_text)
        except Exception:
            cb.record_failure()
            raise
        else:
            cb.record_success()
            return score

    @retry_on_transient(retryable_exceptions=_RERANKER_TRANSIENT_ERRORS, max_retries=3)
    def _score_inner(self, query: str, chunk_text: str) -> float:
        """Call Vertex AI Gemini with retry on transient failures."""
        from vertexai.generative_models import GenerationConfig

        model = self._build_model()
        config = GenerationConfig(
            temperature=0.0,
            max_output_tokens=16,
        )
        prompt = _SCORING_PROMPT_TEMPLATE.format(query=query, chunk_text=chunk_text)
        response = model.generate_content(prompt, generation_config=config)
        return self._parse_score(response.text)

    def _parse_score(self, raw_text: str) -> float:
        """Extract a float in [0.0, 1.0] from the LLM response.

        Returns 0.0 and logs a warning for non-parseable or out-of-range responses.
        NaN and infinity values are treated as non-parseable.
        """
        import math

        text = raw_text.strip()
        try:
            value = float(text)
        except (ValueError, TypeError):
            logger.warning(
                "Non-parseable reranker response: %r (trace=%s)",
                text,
                get_trace_id(),
                extra={"raw_response": text, "trace_id": get_trace_id()},
            )
            return 0.0
        if math.isnan(value) or math.isinf(value):
            logger.warning(
                "Non-parseable reranker response: %r (trace=%s)",
                text,
                get_trace_id(),
                extra={"raw_response": text, "trace_id": get_trace_id()},
            )
            return 0.0
        if value < 0.0 or value > 1.0:
            logger.warning(
                "Reranker score out of range: %f (trace=%s)",
                value,
                get_trace_id(),
                extra={"raw_score": value, "trace_id": get_trace_id()},
            )
            return 0.0
        return value

    def readiness_check(self) -> None:
        """Verify import, project configuration, and SDK init without a model call."""
        self._ensure_initialized()


# ---------------------------------------------------------------------------
# LLMReranker — concurrent scoring orchestrator
# ---------------------------------------------------------------------------


class LLMReranker:
    """Orchestrates concurrent LLM-based chunk scoring with batching and filtering.

    Scores each RetrievalHit via RerankerProvider, applies threshold and top-K
    filtering, and returns results sorted by descending rerank_score. Handles
    per-chunk failures gracefully (0.0 score) and enforces overall timeout.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._provider = RerankerProvider(settings)
        self._executor = ThreadPoolExecutor(max_workers=settings.reranker_max_concurrent)

    def rerank(self, query: str, hits: list[RetrievalHit]) -> list[RankedHit]:
        """Score, filter, and reorder retrieval hits by LLM-assigned relevance.

        Args:
            query: The user's question.
            hits: Candidate chunks from retrieval.

        Returns:
            Filtered and sorted list of RankedHit objects.

        Raises:
            TimeoutError: If overall operation exceeds reranker_timeout_s.
        """
        if not hits:
            return []

        max_concurrent = self._settings.reranker_max_concurrent
        timeout_s = self._settings.reranker_timeout_s

        metrics.observe("rag_reranker_input_count", float(len(hits)))

        t0 = time.perf_counter()
        with timed(logger, "rerank"):
            # Score all chunks in sequential batches
            scored: list[RankedHit] = []
            for batch_start in range(0, len(hits), max_concurrent):
                batch = hits[batch_start : batch_start + max_concurrent]
                scored.extend(self._score_batch(query, batch, timeout_s))

            # Apply threshold filter
            threshold = self._settings.reranker_score_threshold
            if threshold is not None:
                scored = [h for h in scored if h.rerank_score >= threshold]

            # Apply top-K limit
            top_k = self._settings.reranker_top_k
            scored.sort(key=lambda h: h.rerank_score, reverse=True)
            scored = scored[:top_k]

        duration_ms = (time.perf_counter() - t0) * 1000
        metrics.observe("rag_reranker_duration_ms", duration_ms)

        # Emit output metrics
        metrics.observe("rag_reranker_output_count", float(len(scored)))
        if scored:
            scores = [h.rerank_score for h in scored]
            top_score = max(scores)
            min_score = min(scores)
            avg_score = sum(scores) / len(scores)
            metrics.observe("rag_reranker_top_score", top_score)
            logger.info(
                "Rerank scores summary",
                extra={
                    "top_score": top_score,
                    "min_score": min_score,
                    "avg_score": avg_score,
                    "output_count": len(scored),
                    "trace_id": get_trace_id(),
                },
            )

        return scored

    def _score_batch(
        self, query: str, batch: list[RetrievalHit], timeout_s: int
    ) -> list[RankedHit]:
        """Score a single batch of hits concurrently with timeout enforcement."""
        futures_to_hit: dict = {}
        for hit in batch:
            future = self._executor.submit(self._safe_score, query, hit.chunk.text)
            futures_to_hit[future] = hit

        # Wait with overall timeout
        done, not_done = wait(futures_to_hit.keys(), timeout=timeout_s)

        # If any futures didn't complete in time, raise TimeoutError
        if not_done:
            for f in not_done:
                f.cancel()
            raise TimeoutError(
                f"Reranker timeout: {len(not_done)} chunks not scored within {timeout_s}s"
            )

        # Collect results
        results: list[RankedHit] = []
        for future, hit in futures_to_hit.items():
            score = future.result()
            results.append(
                RankedHit(
                    chunk=hit.chunk,
                    score=hit.score,
                    source=hit.source,
                    rerank_score=score,
                )
            )
        return results

    def _safe_score(self, query: str, chunk_text: str) -> float:
        """Score a single chunk, returning 0.0 on any failure."""
        try:
            return self._provider.score_chunk(query, chunk_text)
        except CircuitOpenError:
            raise  # Let circuit-open propagate to trigger fallback
        except Exception:
            logger.warning(
                "Chunk scoring failed, assigning 0.0 (trace=%s)",
                get_trace_id(),
                exc_info=True,
                extra={"trace_id": get_trace_id()},
            )
            return 0.0
