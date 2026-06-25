import json

from rag_system.config import Settings
from rag_system.models import Chunk, EmbeddedChunk
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)


class BedrockTitanEmbedder:
    """Embeds text using Amazon Titan Embed Text V2 via AWS Bedrock."""

    def __init__(self, settings: Settings):
        self._client = settings.boto3_session().client(
            "bedrock-runtime",
            config=settings.bedrock_botocore_config(),
        )
        self._model_id = settings.bedrock_embedding_model_id
        self._dimension = settings.embedding_dimension
        self._max_workers = settings.embedding_max_workers

    @retry_on_transient()
    def _embed_single(self, text: str) -> list[float]:
        """Embed a single text with retry on transient Bedrock failures."""
        body = json.dumps({"inputText": text, "dimensions": self._dimension})
        response = self._client.invoke_model(
            modelId=self._model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        payload = json.loads(response["body"].read())
        return payload["embedding"]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        from concurrent.futures import ThreadPoolExecutor

        logger.debug("Embedding %d text(s) with %s", len(texts), self._model_id)
        input_chars = sum(len(text) for text in texts)
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            embeddings = list(executor.map(self._embed_single, texts))
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
        embedding = self._embed_single(query)
        metrics.increment("rag_query_embedding_total", {"model_id": self._model_id})
        metrics.observe("rag_query_embedding_input_chars", len(query), {"model_id": self._model_id})
        metrics.observe(
            "rag_query_embedding_dimension", len(embedding), {"model_id": self._model_id}
        )
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
