import hashlib
import uuid
from collections import Counter
from typing import Any

from rag_system.chunking import SemanticChunker
from rag_system.config import Settings
from rag_system.embedding import BedrockTitanEmbedder
from rag_system.generation import BedrockNemotronGenerator
from rag_system.models import (
    DocumentRecord,
    DocumentStatus,
    QueryRequest,
    QueryResponse,
    RetrievalHit,
)
from rag_system.observability import get_logger, get_trace_id, metrics, timed
from rag_system.parsing import DocumentParserRouter
from rag_system.queue import IngestionJob, SqsIngestionQueue
from rag_system.rerank import BedrockCohereReranker
from rag_system.retrieval import PineconeHybridIndex
from rag_system.sparse import BM25SparseEncoder
from rag_system.storage import (
    S3ArtifactStore,
    chunks_key,
    document_record_key,
    embedding_manifest_key,
    parsed_key,
)

logger = get_logger(__name__)


class RagService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._store = S3ArtifactStore(settings)
        self._queue = SqsIngestionQueue(settings)
        self._parser: DocumentParserRouter | None = None
        self._chunker: SemanticChunker | None = None
        self._embedder: BedrockTitanEmbedder | None = None
        self._sparse_encoder: BM25SparseEncoder | None = None
        self._index: PineconeHybridIndex | None = None
        self._reranker: BedrockCohereReranker | None = None
        self._generator: BedrockNemotronGenerator | None = None
        self._documents: dict[str, DocumentRecord] = {}
        logger.info(
            "RagService initialised (rerank=%s)",
            "enabled" if settings.rerank_enabled else "disabled",
        )

    @property
    def queue(self) -> SqsIngestionQueue:
        return self._queue

    @property
    def parser(self) -> DocumentParserRouter:
        if self._parser is None:
            self._parser = DocumentParserRouter(self._settings)
        return self._parser

    @property
    def chunker(self) -> SemanticChunker:
        if self._chunker is None:
            self._chunker = SemanticChunker(self._settings)
        return self._chunker

    @property
    def embedder(self) -> BedrockTitanEmbedder:
        if self._embedder is None:
            self._embedder = BedrockTitanEmbedder(self._settings)
        return self._embedder

    @property
    def sparse_encoder(self) -> BM25SparseEncoder:
        if self._sparse_encoder is None:
            self._sparse_encoder = BM25SparseEncoder()
        return self._sparse_encoder

    @property
    def index(self) -> PineconeHybridIndex:
        if self._index is None:
            self._index = PineconeHybridIndex(self._settings)
        return self._index

    @property
    def reranker(self) -> BedrockCohereReranker | None:
        if not self._settings.rerank_enabled:
            return None
        if self._reranker is None:
            self._reranker = BedrockCohereReranker(self._settings)
        return self._reranker

    @property
    def generator(self) -> BedrockNemotronGenerator:
        if self._generator is None:
            self._generator = BedrockNemotronGenerator(self._settings)
        return self._generator

    async def queue_document(self, filename: str, content: bytes) -> DocumentRecord:
        return await self._queue_document(str(uuid.uuid4()), filename, content)

    # Backward-compatible alias
    async def queue_pdf(self, filename: str, content: bytes) -> DocumentRecord:
        return await self.queue_document(filename, content)

    async def update_document(self, document_id: str, filename: str, content: bytes) -> DocumentRecord | None:
        current = self.get_document(document_id)
        if current is None or current.status == DocumentStatus.deleted:
            return None

        with timed(logger, "Pinecone document delete before update", document_id=document_id):
            self.index.delete_document(document_id)
        return await self._queue_document(document_id, filename, content)

    def delete_document(self, document_id: str) -> DocumentRecord | None:
        record = self.get_document(document_id)
        if record is None:
            return None
        if record.status == DocumentStatus.deleted:
            return record

        with timed(logger, "Pinecone document delete", document_id=document_id):
            self.index.delete_document(document_id)
        deleted = record.model_copy(update={"status": DocumentStatus.deleted, "error": None})
        self._save_document_record(deleted)
        metrics.increment("rag_documents_deleted_total")
        logger.info("Document deleted", extra={"document_id": document_id, "version": record.version})
        return deleted

    async def _queue_document(self, document_id: str, filename: str, content: bytes) -> DocumentRecord:
        version = content_hash(content)
        log_extra: dict[str, Any] = {
            "document_id": document_id,
            "version": version,
            "file_name": filename,
        }
        if trace_id := get_trace_id():
            log_extra["trace_id"] = trace_id
        logger.info(
            "Queueing document ingestion: %s (%d bytes)", filename, len(content), extra=log_extra
        )

        s3_uri = self._store.put_raw(document_id, version, filename, content)
        record = DocumentRecord(
            id=document_id,
            title=filename,
            version=version,
            s3_uri=s3_uri,
            status=DocumentStatus.queued,
        )
        self._save_document_record(record)

        job = IngestionJob(
            document_id=document_id,
            version=version,
            filename=filename,
            s3_uri=s3_uri,
            trace_id=get_trace_id(),
        )
        try:
            self._queue.enqueue(job)
        except Exception as exc:
            failed = record.model_copy(
                update={
                    "status": DocumentStatus.failed,
                    "error": f"Failed to enqueue ingestion job: {exc}",
                }
            )
            self._save_document_record(failed)
            raise

        metrics.increment("rag_documents_queued_total")
        logger.info("Document ingestion queued for %s", filename, extra=log_extra)
        return record

    async def ingest_document(self, filename: str, content: bytes) -> DocumentRecord:
        return await self.queue_document(filename, content)

    # Backward-compatible alias
    async def ingest_pdf(self, filename: str, content: bytes) -> DocumentRecord:
        return await self.queue_document(filename, content)

    async def process_document_job(self, job: IngestionJob) -> DocumentRecord:
        record = self.get_document(job.document_id)
        if record is None:
            raise ValueError(f"Document record not found: {job.document_id}")
        if record.status == DocumentStatus.deleted:
            raise ValueError(f"Document was deleted before ingestion: {job.document_id}")
        if record.version != job.version:
            raise ValueError(
                f"Document version mismatch for {job.document_id}: "
                f"record={record.version}, job={job.version}"
            )

        try:
            content = self._store.get_raw(job.document_id, job.version, job.filename)
        except Exception:
            # Fallback: try legacy pdf key for documents ingested before this fix
            try:
                content = self._store.get_pdf(job.document_id, job.version)
            except Exception as exc:
                failed = record.model_copy(
                    update={"status": DocumentStatus.failed, "error": str(exc)}
                )
                self._save_document_record(failed)
                raise
        return await self._run_ingestion(record, content)

    async def _run_ingestion(self, record: DocumentRecord, content: bytes) -> DocumentRecord:
        document_id = record.id
        version = record.version
        filename = record.title
        log_extra: dict[str, Any] = {
            "document_id": document_id,
            "version": version,
            "file_name": filename,
        }
        if trace_id := get_trace_id():
            log_extra["trace_id"] = trace_id
        logger.info(
            "Starting document ingestion: %s (%d bytes)", filename, len(content), extra=log_extra
        )

        try:
            record = record.model_copy(update={"status": DocumentStatus.parsing, "error": None})
            self._save_document_record(record)
            with timed(logger, "document parsing", **log_extra):
                parsed = await self.parser.parse(document_id, version, filename, content)
            self._store.put_json(parsed_key(document_id, version), parsed.model_dump())

            record = record.model_copy(update={"status": DocumentStatus.chunking, "error": None})
            self._save_document_record(record)
            with timed(logger, "semantic chunking", **log_extra):
                chunks = self.chunker.chunk(parsed)
            logger.info(
                "Produced %d chunks",
                len(chunks),
                extra={**log_extra, "chunk_count": len(chunks)},
            )
            self._store.put_chunks(document_id, version, chunks)

            record = record.model_copy(update={"status": DocumentStatus.embedding, "error": None})
            self._save_document_record(record)
            with timed(logger, "dense embedding", **log_extra):
                embedded = self.embedder.embed_chunks(chunks)

            if self._settings.sparse_enabled:
                with timed(logger, "BM25 sparse encoding", **log_extra):
                    sparse_vectors = self.sparse_encoder.encode_documents([c.text for c in chunks])
                for ec, sv in zip(embedded, sparse_vectors, strict=True):
                    ec.sparse_vector = sv

            with timed(logger, "Pinecone upsert", **log_extra):
                self.index.upsert(embedded)

            self._store.put_json(
                embedding_manifest_key(document_id, version),
                {
                    "document_id": document_id,
                    "version": version,
                    "chunk_count": len(chunks),
                    "embedding_model": self._settings.bedrock_embedding_model_id,
                    "sparse_model": "bm25-msmarco-default" if self._settings.sparse_enabled else None,
                    "pinecone_index": self._settings.pinecone_index_name,
                    "chunks_uri": (
                        f"s3://{self._settings.s3_bucket}/"
                        f"{chunks_key(document_id, version)}"
                    ),
                },
            )

            final = record.model_copy(update={"status": DocumentStatus.indexed, "error": None})
            self._save_document_record(final)
            logger.info("Ingestion complete for %s", filename, extra=log_extra)
            return final
        except Exception as exc:
            logger.error(
                "Ingestion failed for %s: %s",
                record.title,
                exc,
                extra=log_extra,
                exc_info=True,
            )
            failed = record.model_copy(
                update={"status": DocumentStatus.failed, "error": str(exc)}
            )
            self._save_document_record(failed)
            raise

    def get_document(self, document_id: str) -> DocumentRecord | None:
        if record := self._documents.get(document_id):
            return record

        payload = self._store.get_json(document_record_key(document_id))
        if payload is None:
            return None

        record = DocumentRecord.model_validate(payload)
        self._documents[document_id] = record
        logger.info(
            "Loaded document record from S3",
            extra={"document_id": document_id, "version": record.version},
        )
        return record

    def _save_document_record(self, record: DocumentRecord) -> None:
        self._documents[record.id] = record
        key = document_record_key(record.id)
        self._store.put_json(key, record.model_dump(mode="json"))
        logger.info(
            "Persisted document record status=%s",
            record.status,
            extra={
                "document_id": record.id,
                "version": record.version,
                "s3_key": key,
            },
        )

    def query(self, request: QueryRequest) -> QueryResponse:
        trace_id = get_trace_id() or str(uuid.uuid4())
        retrieval_mode = "hybrid" if self._settings.sparse_enabled else "dense"
        log_extra: dict[str, Any] = {
            "trace_id": trace_id,
            "query_len": len(request.question),
            "retrieval_mode": retrieval_mode,
        }
        logger.info("Processing query (trace=%s)", trace_id, extra=log_extra)
        metrics.increment("rag_queries_total", {"mode": retrieval_mode})
        metrics.observe("rag_query_length_chars", len(request.question), {"mode": retrieval_mode})

        with timed(logger, "query embedding (dense)", **log_extra):
            query_vector = self.embedder.embed_query(request.question)

        if self._settings.sparse_enabled:
            with timed(logger, "query encoding (sparse/BM25)", **log_extra):
                sparse_query = self.sparse_encoder.encode_query(request.question)
        else:
            sparse_query = None

        sparse_term_count = len(sparse_query.get("indices", [])) if sparse_query else 0
        logger.info(
            "Query vector diagnostics: dense_dim=%d, sparse_terms=%d",
            len(query_vector),
            sparse_term_count,
            extra={
                **log_extra,
                "dense_dimension": len(query_vector),
                "sparse_term_count": sparse_term_count,
            },
        )

        retrieval_operation = (
            "hybrid retrieval (dense+sparse)"
            if self._settings.sparse_enabled
            else "dense retrieval"
        )
        with timed(logger, retrieval_operation, **log_extra):
            hits = self.index.search(
                query_vector=query_vector,
                sparse_vector=sparse_query,
                top_k=self._settings.retrieval_dense_top_k,
                document_ids=request.document_ids,
            )
        logger.info(
            "Retrieved %d hits", len(hits), extra={**log_extra, "hit_count": len(hits)}
        )
        self._observe_retrieval_quality(hits, retrieval_mode, log_extra)

        # Reranking is optional — controlled by RAG_RERANK_ENABLED
        reranker = self.reranker
        if reranker:
            with timed(logger, "reranking", **log_extra):
                top_hits = reranker.rerank(request.question, hits)
            logger.info("Reranked to %d hits", len(top_hits), extra=log_extra)
        else:
            top_hits = hits[: self._settings.rerank_top_k]

        with timed(logger, "answer generation", **log_extra):
            response = self.generator.answer(request.question, top_hits, trace_id)

        self._observe_answer_quality(response, top_hits, retrieval_mode, log_extra)
        logger.info("Query complete (trace=%s)", trace_id, extra=log_extra)
        return response

    def _observe_retrieval_quality(
        self,
        hits: list[RetrievalHit],
        retrieval_mode: str,
        log_extra: dict[str, Any],
    ) -> None:
        if not hits:
            metrics.increment("rag_retrieval_zero_hit_total", {"mode": retrieval_mode})
            logger.warning("Retrieval returned zero hits", extra=log_extra)
            return

        scores = [hit.score for hit in hits]
        top_score = scores[0]
        min_score = min(scores)
        avg_score = sum(scores) / len(scores)
        doc_counts = Counter(hit.chunk.document_id for hit in hits)
        dominant_doc_ratio = max(doc_counts.values()) / len(hits)

        if (
            self._settings.low_top_score_threshold is not None
            and top_score < self._settings.low_top_score_threshold
        ):
            metrics.increment("rag_retrieval_low_top_score_total", {"mode": retrieval_mode})
            logger.warning(
                "Retrieval top score %.4f is below threshold %.4f",
                top_score,
                self._settings.low_top_score_threshold,
                extra={
                    **log_extra,
                    "top_score": top_score,
                    "min_score": min_score,
                    "avg_score": avg_score,
                },
            )

        metrics.observe(
            "rag_retrieval_dominant_doc_ratio",
            dominant_doc_ratio,
            {"mode": retrieval_mode},
        )
        logger.info(
            "Retrieval quality: top=%.4f min=%.4f avg=%.4f unique_docs=%d "
            "dominant_doc_ratio=%.2f",
            top_score,
            min_score,
            avg_score,
            len(doc_counts),
            dominant_doc_ratio,
            extra={
                **log_extra,
                "top_score": top_score,
                "min_score": min_score,
                "avg_score": avg_score,
                "unique_doc_count": len(doc_counts),
                "dominant_doc_ratio": dominant_doc_ratio,
            },
        )

    def _observe_answer_quality(
        self,
        response: QueryResponse,
        top_hits: list[RetrievalHit],
        retrieval_mode: str,
        log_extra: dict[str, Any],
    ) -> None:
        labels = {"mode": retrieval_mode, "evidence_status": response.evidence_status}
        metrics.increment("rag_evidence_status_total", {"status": response.evidence_status})
        metrics.observe("rag_answer_citation_count", len(response.citations), labels)
        metrics.observe("rag_answer_context_hit_count", len(top_hits), labels)
        if not response.citations:
            metrics.increment("rag_answer_without_citations_total", {"mode": retrieval_mode})
            logger.warning("Answer returned without citations", extra=log_extra)
        logger.info(
            "Answer quality: evidence_status=%s citations=%d context_hits=%d",
            response.evidence_status,
            len(response.citations),
            len(top_hits),
            extra={
                **log_extra,
                "evidence_status": response.evidence_status,
                "citation_count": len(response.citations),
                "hit_count": len(top_hits),
            },
        )


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:24]
