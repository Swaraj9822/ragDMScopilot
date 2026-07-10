import time
from typing import Any

from pinecone import Pinecone

from rag_system.config import Settings
from rag_system.models import Chunk, EmbeddedChunk, RetrievalHit
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)


class PineconeHybridIndex:
    def __init__(self, settings: Settings):
        self._index = Pinecone(api_key=settings.pinecone_api_key).Index(
            settings.pinecone_index_name
        )
        # Config validator enforces a sane [1, 1000] range for this setting.
        self._upsert_batch_size = settings.pinecone_upsert_batch_size
        logger.info("Connected to Pinecone index '%s'", settings.pinecone_index_name)

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        vectors = []
        for item in embedded_chunks:
            metadata = {
                **item.chunk.metadata,
                "document_id": item.chunk.document_id,
                "version": item.chunk.version,
                "text": item.chunk.text,
                "page_start": item.chunk.page_start,
                "page_end": item.chunk.page_end,
                "section_path": item.chunk.section_path,
            }
            # Pinecone rejects null metadata values — strip them out
            metadata = {k: v for k, v in metadata.items() if v is not None}
            vector: dict[str, Any] = {
                "id": item.chunk.id,
                "values": item.dense_vector,
                "metadata": metadata,
            }
            if item.sparse_vector:
                vector["sparse_values"] = item.sparse_vector
            vectors.append(vector)

        if vectors:
            sparse_count = sum(1 for v in vectors if "sparse_values" in v)
            missing_sparse_count = len(vectors) - sparse_count
            dense_dimensions = sorted({len(item.dense_vector) for item in embedded_chunks})
            sparse_term_counts = [
                len(vector["sparse_values"].get("indices", []))
                for vector in vectors
                if "sparse_values" in vector
            ]
            avg_sparse_terms = (
                sum(sparse_term_counts) / len(sparse_term_counts)
                if sparse_term_counts
                else 0.0
            )
            dense_dimension = dense_dimensions[0] if len(dense_dimensions) == 1 else None
            logger.info(
                "Upserting %d vectors to Pinecone (%d with sparse, %d missing sparse)",
                len(vectors),
                sparse_count,
                missing_sparse_count,
                extra={
                    "vector_count": len(vectors),
                    "sparse_count": sparse_count,
                    "missing_sparse_count": missing_sparse_count,
                    "dense_dimension": dense_dimension,
                    "sparse_term_count": round(avg_sparse_terms),
                },
            )
            metrics.observe("rag_pinecone_upsert_vectors", len(vectors))
            metrics.observe("rag_pinecone_upsert_sparse_vectors", sparse_count)
            metrics.observe("rag_pinecone_upsert_missing_sparse_vectors", missing_sparse_count)
            metrics.observe("rag_pinecone_upsert_avg_sparse_terms", avg_sparse_terms)
            # Pinecone caps request size (~2MB); a large document can exceed it
            # in a single call and fail the whole ingestion. Upsert in bounded
            # batches so each request stays within limits. The retry lives on
            # _upsert_batch (not on this method) so a transient failure re-sends
            # only the offending batch instead of re-running every batch —
            # upserts are idempotent, so a whole-method retry was correctness-safe
            # but doubled work and Pinecone load under throttling.
            batch_size = self._upsert_batch_size
            batch_count = (len(vectors) + batch_size - 1) // batch_size
            start = time.perf_counter()
            for offset in range(0, len(vectors), batch_size):
                self._upsert_batch(vectors[offset : offset + batch_size])
            duration_ms = (time.perf_counter() - start) * 1000
            metrics.observe("rag_pinecone_upsert_duration_ms", duration_ms)
            metrics.observe("rag_pinecone_upsert_batches", batch_count)
            logger.info(
                "Upsert complete (%d vectors in %d batch(es), %.0fms)",
                len(vectors),
                batch_count,
                duration_ms,
                extra={"vector_count": len(vectors), "duration_ms": duration_ms},
            )

    @retry_on_transient()
    def _upsert_batch(self, batch: list[dict[str, Any]]) -> None:
        """Upsert a single bounded batch, retrying only this batch on a
        transient failure. Kept separate from :meth:`upsert` so a retry does not
        re-send batches that already succeeded."""
        self._index.upsert(vectors=batch)

    @retry_on_transient()
    def delete_document(self, document_id: str) -> None:
        logger.info("Deleting Pinecone vectors for document", extra={"document_id": document_id})
        start = time.perf_counter()
        self._index.delete(filter={"document_id": {"$eq": document_id}})
        duration_ms = (time.perf_counter() - start) * 1000
        metrics.increment("rag_pinecone_delete_document_total")
        metrics.observe("rag_pinecone_delete_document_duration_ms", duration_ms)
        logger.info(
            "Deleted Pinecone vectors for document (%.0fms)",
            duration_ms,
            extra={"document_id": document_id, "duration_ms": duration_ms},
        )

    @retry_on_transient()
    def delete_document_version(self, document_id: str, version: str) -> None:
        """Delete only the vectors of one document *version*.

        Used to clean up the partial vectors left by a failed or superseded
        ingestion without touching the currently published version (whose
        vectors carry a different ``version`` in their metadata).
        """
        logger.info(
            "Deleting Pinecone vectors for document version",
            extra={"document_id": document_id, "version": version},
        )
        self._index.delete(
            filter={"document_id": {"$eq": document_id}, "version": {"$eq": version}}
        )
        metrics.increment("rag_pinecone_delete_document_version_total")

    @retry_on_transient()
    def delete_document_except_version(self, document_id: str, keep_version: str) -> None:
        """Delete every vector of a document except the published version.

        Called after an ingestion atomically switches the active version, to
        garbage-collect the previous version's vectors and any lingering partials
        from earlier failed/superseded attempts. Best-effort: the active-version
        search gate already hides non-published vectors, so a cleanup failure is
        not correctness-critical.
        """
        logger.info(
            "Deleting superseded Pinecone vectors for document",
            extra={"document_id": document_id, "keep_version": keep_version},
        )
        self._index.delete(
            filter={"document_id": {"$eq": document_id}, "version": {"$ne": keep_version}}
        )
        metrics.increment("rag_pinecone_delete_superseded_total")

    @retry_on_transient()
    def search(
        self,
        query_vector: list[float],
        top_k: int,
        document_ids: list[str] | None = None,
        sparse_vector: dict[str, Any] | None = None,
    ) -> list[RetrievalHit]:
        if document_ids is None:
            # No scope requested → search the whole index.
            filters = None
        elif len(document_ids) == 0:
            # An explicitly empty scope means "restricted to zero documents"
            # (e.g. a non-operator with no accessible documents). Return no hits
            # rather than falling through to an unscoped whole-index search —
            # that fall-through would leak documents the caller may not read.
            return []
        else:
            filters = {"document_id": {"$in": document_ids}}

        query_kwargs: dict[str, Any] = {
            "vector": query_vector,
            "top_k": top_k,
            "include_metadata": True,
        }
        if sparse_vector is not None:
            query_kwargs["sparse_vector"] = sparse_vector
        if filters is not None:
            query_kwargs["filter"] = filters

        mode = "hybrid" if sparse_vector else "dense"
        dense_dimension = len(query_vector)
        sparse_term_count = len(sparse_vector.get("indices", [])) if sparse_vector else 0
        doc_filter_count = len(document_ids or [])
        logger.info(
            "Searching Pinecone [%s] (top_k=%d, dense_dim=%d, sparse_terms=%d, filter_docs=%d)",
            mode,
            top_k,
            dense_dimension,
            sparse_term_count,
            doc_filter_count,
            extra={
                "top_k": top_k,
                "retrieval_mode": mode,
                "dense_dimension": dense_dimension,
                "sparse_term_count": sparse_term_count,
                "doc_filter_count": doc_filter_count,
            },
        )

        query_start = time.perf_counter()
        response = self._index.query(**query_kwargs)
        duration_ms = (time.perf_counter() - query_start) * 1000
        metrics.observe("rag_pinecone_query_duration_ms", duration_ms, {"mode": mode})
        metrics.observe("rag_query_dense_dimension", dense_dimension, {"mode": mode})
        metrics.observe("rag_query_sparse_terms", sparse_term_count, {"mode": mode})

        matches = list(response.matches or [])
        scores = [float(match.score) for match in matches]
        top_score = scores[0] if scores else None
        min_score = min(scores) if scores else None
        avg_score = sum(scores) / len(scores) if scores else None
        top_match_ids = [match.id for match in matches[:5]]

        hits: list[RetrievalHit] = []
        unique_doc_ids: set[str] = set()
        for match in matches:
            metadata = dict(match.metadata or {})
            document_id = metadata["document_id"]
            unique_doc_ids.add(document_id)
            chunk = Chunk(
                id=match.id,
                document_id=document_id,
                version=metadata["version"],
                text=metadata["text"],
                page_start=metadata.get("page_start"),
                page_end=metadata.get("page_end"),
                section_path=metadata.get("section_path") or [],
                metadata=metadata,
            )
            hits.append(RetrievalHit(chunk=chunk, score=float(match.score), source="pinecone"))
        if top_score is not None:
            metrics.observe("rag_retrieval_top_score", top_score, {"mode": mode})
            metrics.observe("rag_retrieval_min_score", min_score or 0.0, {"mode": mode})
            metrics.observe("rag_retrieval_avg_score", avg_score or 0.0, {"mode": mode})
        metrics.observe("rag_retrieval_hit_count", len(hits), {"mode": mode})
        metrics.observe("rag_retrieval_unique_doc_count", len(unique_doc_ids), {"mode": mode})
        logger.info(
            "Pinecone returned %d hits (mode=%s, top_score=%s, min_score=%s, avg_score=%s)",
            len(hits),
            mode,
            _fmt_score(top_score),
            _fmt_score(min_score),
            _fmt_score(avg_score),
            extra={
                "hit_count": len(hits),
                "retrieval_mode": mode,
                "top_score": top_score,
                "min_score": min_score,
                "avg_score": avg_score,
                "unique_doc_count": len(unique_doc_ids),
                "top_match_ids": top_match_ids,
                "duration_ms": duration_ms,
            },
        )
        return hits


def _fmt_score(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.4f}"
