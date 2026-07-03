import math
import os
from concurrent.futures import ThreadPoolExecutor

from rag_system.config import Settings
from rag_system.models import Chunk, EmbeddedChunk
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)

#: Vertex embedding task types. Documents and queries are embedded with matched
#: retrieval task types so question/passage vectors land in the same space.
_TASK_TYPE_DOCUMENT = "RETRIEVAL_DOCUMENT"
_TASK_TYPE_QUERY = "RETRIEVAL_QUERY"


class GeminiEmbedder:
    """Embeds text using Google's ``gemini-embedding-001`` model on Vertex AI.

    Uses the same ``google-genai`` client and Application Default Credentials as
    the text-generation LLM. The Vertex embedding API embeds one text per
    request, so a document's chunks are embedded via a bounded concurrent
    fan-out (mirroring the previous Titan path). Output vectors are L2-normalized
    so dot-product similarity in Pinecone (the metric required for sparse+dense
    hybrid search) matches cosine similarity.
    """

    def __init__(self, settings: Settings):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "google-genai is not installed. Run `pip install google-genai` "
                "to use the Gemini embedding provider."
            ) from exc

        if not settings.gcp_project_id:
            raise RuntimeError(
                "GCP_PROJECT_ID must be set to use the Gemini embedder on Vertex AI."
            )

        # Honour an explicit service-account key path if provided; otherwise the
        # SDK falls back to Application Default Credentials.
        if settings.google_application_credentials:
            os.environ.setdefault(
                "GOOGLE_APPLICATION_CREDENTIALS",
                settings.google_application_credentials,
            )

        self._types = types
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
        self._model_id = settings.embedding_model_id
        self._dimension = settings.embedding_dimension
        # Config validator already enforces [1, 1000]; no local clamp needed.
        self._max_workers = settings.embedding_max_workers
        # Instance-level pool, created once on first use and reused across
        # documents, so a multi-hundred-chunk upload no longer pays thread
        # create/teardown on every embed call. Skipped entirely in the serial
        # path (max_workers <= 1 or a single text).
        self._executor: ThreadPoolExecutor | None = None

    @retry_on_transient()
    def _embed_single(self, text: str, task_type: str = _TASK_TYPE_DOCUMENT) -> list[float]:
        """Embed a single text with retry on transient Vertex failures.

        ``task_type`` defaults to ``RETRIEVAL_DOCUMENT`` so the document fan-out
        can call this with a single positional argument; queries pass
        ``RETRIEVAL_QUERY`` explicitly.
        """
        response = self._client.models.embed_content(
            model=self._model_id,
            contents=text,
            config=self._types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self._dimension,
            ),
        )
        values = list(response.embeddings[0].values)
        return _l2_normalize(values)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        logger.debug("Embedding %d text(s) with %s", len(texts), self._model_id)
        input_chars = sum(len(text) for text in texts)
        embeddings = self._embed_many(texts)
        logger.info(
            "Embedded %d text(s) via %s",
            len(texts),
            self._model_id,
            extra={
                "model_id": self._model_id,
                "vector_count": len(texts),
                "embedding_input_chars": input_chars,
                "dense_dimension": self._dimension,
            },
        )
        metrics.increment("rag_embedding_texts_total", {"model_id": self._model_id}, len(texts))
        metrics.observe("rag_embedding_batch_size", len(texts), {"model_id": self._model_id})
        metrics.observe("rag_embedding_input_chars", input_chars, {"model_id": self._model_id})
        return embeddings

    def _embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts, in input order, fanning out concurrent requests.

        The Vertex embedding API embeds one text per request, so a document's
        chunks were previously embedded in a serial loop — N sequential network
        round-trips. A bounded thread pool issues those requests concurrently
        while ``executor.map`` preserves input order, so the returned vectors
        still line up with ``texts``. Each call keeps its own transient-retry via
        ``_embed_single``. The genai client is safe for concurrent calls.
        """
        if len(texts) <= 1 or self._max_workers <= 1:
            return [self._embed_single(text) for text in texts]
        return list(self._get_executor().map(self._embed_single, texts))

    def _get_executor(self) -> ThreadPoolExecutor:
        """Return the shared embed pool, creating it once on first use.

        Uses ``getattr`` so instances built via ``object.__new__`` (tests) that
        never ran ``__init__`` still work.
        """
        executor = getattr(self, "_executor", None)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="embed"
            )
            self._executor = executor
        return executor

    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        if not chunks:
            return []
        embeddings = self._embed([chunk.text for chunk in chunks])
        return [
            EmbeddedChunk(chunk=chunk, dense_vector=embedding)
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]

    def embed_query(self, query: str) -> list[float]:
        logger.debug("Embedding query (%d chars)", len(query))
        embedding = self._embed_single(query, _TASK_TYPE_QUERY)
        metrics.increment("rag_query_embedding_total", {"model_id": self._model_id})
        metrics.observe("rag_query_embedding_input_chars", len(query), {"model_id": self._model_id})
        metrics.observe("rag_query_embedding_dimension", len(embedding), {"model_id": self._model_id})
        logger.info(
            "Embedded query via %s (chars=%d, dim=%d)",
            self._model_id,
            len(query),
            len(embedding),
            extra={
                "model_id": self._model_id,
                "embedding_input_chars": len(query),
                "dense_dimension": len(embedding),
            },
        )
        return embedding


def _l2_normalize(vector: list[float]) -> list[float]:
    """Scale a vector to unit L2 norm.

    gemini-embedding-001 already returns unit-length vectors at 3072 dims, but
    truncated (Matryoshka) dimensions are not normalized. Normalizing
    unconditionally keeps dot-product similarity equal to cosine similarity for
    any configured ``embedding_dimension``. A zero vector is returned unchanged.
    """
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]
