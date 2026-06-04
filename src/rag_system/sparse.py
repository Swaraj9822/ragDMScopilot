"""BM25 sparse vector encoder for Pinecone hybrid retrieval."""

from __future__ import annotations

from typing import Any

from pinecone_text.sparse import BM25Encoder

from rag_system.observability import get_logger, metrics

logger = get_logger(__name__)


class BM25SparseEncoder:
    """Generate BM25 sparse vectors using a pre-trained MS MARCO model.

    These sparse vectors enable lexical (keyword) matching alongside dense
    semantic vectors in Pinecone hybrid search.
    """

    def __init__(self) -> None:
        logger.info("Loading pre-trained BM25 encoder (MS MARCO)")
        self._encoder = BM25Encoder.default()
        logger.info("BM25 encoder ready")

    def encode_documents(self, texts: list[str]) -> list[dict[str, Any]]:
        """Return sparse vectors for document chunks."""
        if not texts:
            return []
        sparse_vectors = self._encoder.encode_documents(texts)
        logger.debug("Encoded %d document(s) into BM25 sparse vectors", len(texts))
        normalised = [_normalise(sv) for sv in sparse_vectors]
        term_counts = [len(sv["indices"]) for sv in normalised]
        avg_terms = sum(term_counts) / len(term_counts) if term_counts else 0.0
        metrics.observe("rag_sparse_document_count", len(normalised), {"model_id": "bm25"})
        metrics.observe("rag_sparse_document_avg_terms", avg_terms, {"model_id": "bm25"})
        logger.info(
            "Encoded %d document(s) into BM25 sparse vectors (avg_terms=%.1f)",
            len(normalised),
            avg_terms,
            extra={"sparse_count": len(normalised), "sparse_term_count": round(avg_terms)},
        )
        return normalised

    def encode_query(self, query: str) -> dict[str, Any]:
        """Return a single sparse vector for a query string."""
        sv = self._encoder.encode_queries([query])[0]
        normalised = _normalise(sv)
        term_count = len(normalised["indices"])
        metrics.observe("rag_sparse_query_terms", term_count, {"model_id": "bm25"})
        logger.info(
            "Encoded query into BM25 sparse vector (%d terms)",
            term_count,
            extra={"sparse_term_count": term_count},
        )
        return normalised


def _normalise(sv: Any) -> dict[str, Any]:
    """Accept both dict and object forms returned by different pinecone-text versions."""
    if isinstance(sv, dict):
        return {"indices": sv["indices"], "values": sv["values"]}
    return {"indices": list(sv.indices), "values": list(sv.values)}
