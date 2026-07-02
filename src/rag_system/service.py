import asyncio
import contextvars
import hashlib
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterator

from rag_system.chunking import DocumentChunker
from rag_system.config import Settings
from rag_system.embedding import BedrockTitanEmbedder
from rag_system.generation import GroundedAnswerGenerator
from rag_system.models import (
    DocumentRecord,
    DocumentStatus,
    QueryFeedbackRecord,
    QueryFeedbackRequest,
    QueryRequest,
    QueryResponse,
    QueryTraceHit,
    QueryTraceRecord,
    RetrievalHit,
)
from rag_system.observability import (
    get_logger,
    get_trace_id,
    is_unified_active,
    metrics,
    timed,
)
from rag_system.observability_tracing import get_active_trace_id, record_query_summary
from rag_system.parsing import DocumentParserRouter
from rag_system.queue import IngestionJob, SqsIngestionQueue
from rag_system.rerank import BedrockCohereReranker
from rag_system.retrieval import PineconeHybridIndex
from rag_system.sparse import BM25SparseEncoder
from rag_system.storage import (
    PreconditionFailed,
    S3ArtifactStore,
    chunks_key,
    document_record_key,
    embedding_manifest_key,
    parsed_key,
    query_feedback_key,
    query_trace_key,
)

logger = get_logger(__name__)

#: Bounded retries for the document-record compare-and-set loop. A conflict
#: means another writer (API delete vs. ingestion worker) updated the record
#: between our read and write; we reload and re-evaluate. In practice at most
#: two writers contend, so a handful of attempts is ample headroom.
_MAX_RECORD_CAS_ATTEMPTS = 5


class DocumentDeletedError(RuntimeError):
    """Raised when a live status write would resurrect a deleted document.

    The document-record write path treats ``deleted`` as terminal: once a record
    is deleted (e.g. via ``DELETE /documents/{id}``), the ingestion worker must
    not overwrite it with ``indexed``/``parsing``/etc. Racing writers previously
    did exactly that, resurrecting a document whose vectors were already gone.
    """

    def __init__(self, document_id: str) -> None:
        super().__init__(
            f"Document {document_id} was deleted; refusing to resurrect it"
        )
        self.document_id = document_id


class StaleIngestionError(RuntimeError):
    """Raised when an ingestion write targets a superseded document version.

    Every upload/replacement stamps the record with a new content-hash version.
    While version A is being ingested, a newer upload B replaces the stored
    record (status ``queued``, version B). Any later progress/terminal write
    from A (``parsing``..``indexed``/``failed``) would otherwise clobber B's
    record with A's stale version — and B's own job would then read the wrong
    version, fail its version check, and churn to the DLQ.

    Refusing the stale write with this error keeps B's record intact and lets
    the worker treat A as a completed no-op. It is also raised up front when a
    job's version no longer matches the current record, so an already-superseded
    job is dropped cleanly instead of being retried to the DLQ.
    """

    def __init__(
        self, document_id: str, attempted_version: str, current_version: str | None
    ) -> None:
        super().__init__(
            f"Document {document_id} ingestion for version {attempted_version} "
            f"is stale; current stored version is {current_version}"
        )
        self.document_id = document_id
        self.attempted_version = attempted_version
        self.current_version = current_version


# Bounded pool for best-effort, off-request-path query-trace persistence. A
# fixed worker count caps concurrent background writes instead of spawning one
# unbounded thread per query under load; trace writes are short S3 puts, so a
# small pool drains them quickly. Threads are daemon so they never hold up
# process exit beyond a brief drain.
_TRACE_PERSIST_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="trace-writer"
)


class RagService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._store = S3ArtifactStore(settings)
        self._queue = SqsIngestionQueue(settings)
        self._parser: DocumentParserRouter | None = None
        self._chunker: DocumentChunker | None = None
        self._embedder: BedrockTitanEmbedder | None = None
        self._sparse_encoder: BM25SparseEncoder | None = None
        self._index: PineconeHybridIndex | None = None
        self._reranker: BedrockCohereReranker | None = None
        self._generator: GroundedAnswerGenerator | None = None
        self._documents: dict[str, DocumentRecord] = {}
        # Shared pool for the Documents-tab listing fan-out (created lazily).
        self._document_list_executor: ThreadPoolExecutor | None = None
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
    def chunker(self) -> DocumentChunker:
        if self._chunker is None:
            self._chunker = DocumentChunker(self._settings)
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
    def generator(self) -> GroundedAnswerGenerator:
        if self._generator is None:
            self._generator = GroundedAnswerGenerator(self._settings)
        return self._generator

    async def queue_document(self, filename: str, content: bytes) -> DocumentRecord:
        return await self._queue_document(str(uuid.uuid4()), filename, content)

    # Backward-compatible alias
    async def queue_pdf(self, filename: str, content: bytes) -> DocumentRecord:
        return await self.queue_document(filename, content)

    async def update_document(self, document_id: str, filename: str, content: bytes) -> DocumentRecord | None:
        current = await asyncio.to_thread(self.get_document, document_id)
        if current is None or current.status == DocumentStatus.deleted:
            return None

        # Do NOT delete the current vectors here. The previously published
        # version must stay searchable until the replacement is fully ingested
        # and atomically published; the new ingestion cleans up superseded
        # vectors only after it switches the active version. Pre-deleting left a
        # gap where a failed replacement destroyed the last good version.
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

        s3_uri = await asyncio.to_thread(
            self._store.put_raw, document_id, version, filename, content
        )
        # Preserve the currently published version across a replacement so the
        # existing vectors stay searchable while the new version ingests. A
        # brand-new document has no prior record and therefore no active version.
        # The lookup is skipped for write-only stores (minimal test doubles);
        # real S3 always supports reads, and a fresh upload has nothing to carry.
        existing = None
        if hasattr(self._store, "get_json"):
            existing = await asyncio.to_thread(self.get_document, document_id)
        active_version = self._published_version_of(existing)
        record = DocumentRecord(
            id=document_id,
            title=filename,
            version=version,
            s3_uri=s3_uri,
            status=DocumentStatus.queued,
            active_version=active_version,
        )
        await asyncio.to_thread(self._save_document_record, record)

        job = IngestionJob(
            document_id=document_id,
            version=version,
            filename=filename,
            s3_uri=s3_uri,
            trace_id=get_active_trace_id(),
        )
        try:
            await asyncio.to_thread(self._queue.enqueue, job)
        except Exception as exc:
            failed = record.model_copy(
                update={
                    "status": DocumentStatus.failed,
                    "error": f"Failed to enqueue ingestion job: {exc}",
                }
            )
            await asyncio.to_thread(self._save_document_record, failed)
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
            # Terminal state: the document was deleted before this job ran, so
            # it can never succeed. Signal with DocumentDeletedError (not a bare
            # ValueError) so the worker treats it as a no-op and removes the
            # message instead of retrying it to the DLQ.
            raise DocumentDeletedError(job.document_id)
        if record.version != job.version:
            # The stored record has moved on to a different version (a newer
            # upload replaced this one). This job is stale: it can never produce
            # the current version, so signal StaleIngestionError — the worker
            # drops it as a no-op instead of retrying it to the DLQ.
            raise StaleIngestionError(job.document_id, job.version, record.version)

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

        from rag_system.observability_tracing import get_span_recorder

        recorder = get_span_recorder()

        try:
            # --- Parsing stage (R12.2, R12.4, R12.5, R12.7) ---
            record = record.model_copy(update={"status": DocumentStatus.parsing, "error": None})
            await asyncio.to_thread(self._save_document_record, record)
            with recorder.record_span("document parsing") as span:
                parsed = await self.parser.parse(document_id, version, filename, content)
                recorder.set_ingestion_attributes(
                    span,
                    document_id=document_id if document_id is not None else None,
                    document_version=version if version is not None else None,
                    source_filename=filename,
                )
            await asyncio.to_thread(
                self._store.put_json, parsed_key(document_id, version), parsed.model_dump()
            )

            # --- Chunking stage (R12.2, R12.4, R12.5, R12.7) ---
            record = record.model_copy(update={"status": DocumentStatus.chunking, "error": None})
            await asyncio.to_thread(self._save_document_record, record)
            with recorder.record_span("chunking") as span:
                chunks = await asyncio.to_thread(self.chunker.chunk, parsed)
                recorder.set_ingestion_attributes(
                    span,
                    document_id=document_id if document_id is not None else None,
                    document_version=version if version is not None else None,
                    source_filename=filename,
                )
            logger.info(
                "Produced %d chunks",
                len(chunks),
                extra={**log_extra, "chunk_count": len(chunks)},
            )
            await asyncio.to_thread(self._store.put_chunks, document_id, version, chunks)

            # --- Embedding stage (R12.2, R12.4, R12.5, R12.7) ---
            record = record.model_copy(update={"status": DocumentStatus.embedding, "error": None})
            await asyncio.to_thread(self._save_document_record, record)
            with recorder.record_span("dense embedding") as span:
                embedded = await asyncio.to_thread(self.embedder.embed_chunks, chunks)
                recorder.set_ingestion_attributes(
                    span,
                    document_id=document_id if document_id is not None else None,
                    document_version=version if version is not None else None,
                    source_filename=filename,
                )

            if self._settings.sparse_enabled:
                with timed(logger, "BM25 sparse encoding", **log_extra):
                    sparse_vectors = await asyncio.to_thread(
                        self.sparse_encoder.encode_documents, [c.text for c in chunks]
                    )
                for ec, sv in zip(embedded, sparse_vectors, strict=True):
                    ec.sparse_vector = sv

            # --- Indexing stage (R12.2, R12.4, R12.5, R12.7) ---
            with recorder.record_span("Pinecone upsert") as span:
                await asyncio.to_thread(self.index.upsert, embedded)
                recorder.set_ingestion_attributes(
                    span,
                    document_id=document_id if document_id is not None else None,
                    document_version=version if version is not None else None,
                    source_filename=filename,
                )

            await asyncio.to_thread(
                self._store.put_json,
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

            final = record.model_copy(
                update={
                    "status": DocumentStatus.indexed,
                    "active_version": version,
                    "error": None,
                }
            )
            # This record write is the atomic publication switch: until it lands,
            # active_version still points at the previous version (or None), so
            # the vectors upserted above are not yet searchable (see
            # _active_version_for). After it lands, garbage-collect the previous
            # version's vectors and any partials from earlier attempts.
            await asyncio.to_thread(self._save_document_record, final)
            logger.info("Ingestion complete for %s", filename, extra=log_extra)
            self._cleanup_superseded_vectors(document_id, version, log_extra)
            return final
        except DocumentDeletedError:
            # The document was deleted (DELETE /documents/{id}) while this job
            # was mid-flight. The record write was refused to avoid resurrecting
            # it; roll back any vectors we may have already upserted so the
            # deleted state is consistent, then stop without marking failed.
            return self._abort_ingestion_for_deleted(document_id, log_extra)
        except StaleIngestionError:
            # A newer upload replaced this document while the job was mid-flight.
            # The record write was refused to avoid clobbering the newer version;
            # stop without marking failed and without deleting vectors (the newer
            # version owns the document now — a document_id-wide delete here would
            # also remove its vectors; publication-level cleanup is handled by the
            # version-scoped index work).
            return self._abort_ingestion_for_superseded(document_id, log_extra)
        except Exception as exc:
            # R12.6: The record_span context manager automatically sets the
            # failing stage span status to "error" and records the exception.
            # The root span opened by the worker also gets status "error"
            # because the exception propagates out of start_trace.
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
            try:
                await asyncio.to_thread(self._save_document_record, failed)
            except DocumentDeletedError:
                # Deletion also won the race against the failure write — the
                # document is intentionally gone, so don't record a failure.
                return self._abort_ingestion_for_deleted(document_id, log_extra)
            except StaleIngestionError:
                # A newer upload won the race against the failure write — the
                # newer version owns the record, so don't overwrite it with this
                # stale failure.
                return self._abort_ingestion_for_superseded(document_id, log_extra)
            # Remove this failed version's partial vectors so they can never be
            # published; the previously published version (a different version)
            # is untouched and stays searchable.
            self._cleanup_failed_version_vectors(document_id, version, log_extra)
            raise

    def _abort_ingestion_for_deleted(
        self, document_id: str, log_extra: dict[str, Any]
    ) -> DocumentRecord:
        logger.info(
            "Document deleted during ingestion; rolling back vectors and stopping",
            extra=log_extra,
        )
        try:
            self.index.delete_document(document_id)
        except Exception:
            logger.warning(
                "Vector rollback after concurrent delete failed",
                extra=log_extra,
                exc_info=True,
            )
        metrics.increment("rag_documents_deleted_during_ingestion_total")
        current = self.get_document(document_id)
        if current is not None:
            return current
        # Store no longer has the record; synthesise the terminal deleted view.
        return DocumentRecord(
            id=document_id,
            title="",
            version="",
            s3_uri="",
            status=DocumentStatus.deleted,
        )

    def _abort_ingestion_for_superseded(
        self, document_id: str, log_extra: dict[str, Any]
    ) -> DocumentRecord:
        """Stop a stale ingestion whose version was replaced by a newer upload.

        Unlike the deleted-abort, this does NOT delete vectors: the newer
        version now owns the document and a ``document_id``-wide delete would
        remove its vectors too. We simply return the current (newer) record so
        the worker completes the stale job as a no-op.
        """
        logger.info(
            "Ingestion superseded by a newer document version; stopping without "
            "overwriting the current record",
            extra=log_extra,
        )
        metrics.increment("rag_documents_ingestion_superseded_total")
        current = self.get_document(document_id)
        if current is not None:
            return current
        # No canonical record to return (should not happen: the newer upload
        # wrote one). Synthesise a neutral non-terminal view.
        return DocumentRecord(
            id=document_id,
            title="",
            version="",
            s3_uri="",
            status=DocumentStatus.queued,
        )

    def _cleanup_superseded_vectors(
        self, document_id: str, keep_version: str, log_extra: dict[str, Any]
    ) -> None:
        """Best-effort GC of non-published vectors after a publication switch.

        Runs after the active version is switched, so a failure here is not
        correctness-critical: the search gate already hides any non-active
        vectors that remain. Swallow errors so a cleanup hiccup never fails an
        ingestion that already published successfully.
        """
        try:
            self.index.delete_document_except_version(document_id, keep_version)
        except Exception:
            logger.warning(
                "Post-publish vector cleanup failed; superseded vectors remain "
                "(hidden by the active-version gate until a later cleanup)",
                extra=log_extra,
                exc_info=True,
            )

    def _cleanup_failed_version_vectors(
        self, document_id: str, version: str, log_extra: dict[str, Any]
    ) -> None:
        """Best-effort removal of a failed ingestion's partial vectors."""
        try:
            self.index.delete_document_version(document_id, version)
        except Exception:
            logger.warning(
                "Failed-ingestion vector cleanup failed; partial vectors remain "
                "(hidden by the active-version gate)",
                extra=log_extra,
                exc_info=True,
            )

    @staticmethod
    def _published_version_of(record: DocumentRecord | None) -> str | None:
        """Return the searchable (published) version for a record, or ``None``.

        Falls back to ``version`` for legacy ``indexed`` records written before
        the ``active_version`` pointer existed, so they keep serving results.
        """
        if record is None or record.status == DocumentStatus.deleted:
            return None
        if record.active_version:
            return record.active_version
        if record.status == DocumentStatus.indexed:
            return record.version
        return None

    def get_document(self, document_id: str) -> DocumentRecord | None:
        # Only trust the in-memory cache for records in a TERMINAL state. While a
        # document is still being ingested, its canonical record is owned by the
        # ingestion worker (a separate process) which advances the status in the
        # shared store. Serving the cached non-terminal copy here would report a
        # stale status forever (e.g. "queued" even after ingestion finished),
        # because this process's cache is never notified of the worker's writes.
        cached = self._documents.get(document_id)
        if cached is not None and cached.status in (
            DocumentStatus.indexed,
            DocumentStatus.failed,
            DocumentStatus.deleted,
        ):
            return cached

        payload = self._store.get_json(document_record_key(document_id))
        if payload is None:
            # No canonical record in the store; fall back to any cached copy.
            # Tradeoff: if another process deleted the record (removing the S3
            # object) this can briefly serve a stale non-deleted cached copy
            # until the cache entry is replaced. That is acceptable here —
            # deletes also write a terminal `deleted` record (see
            # delete_document), so the object normally still exists and this
            # branch is only hit when there is genuinely no canonical state to
            # trust over the cache.
            return cached

        record = DocumentRecord.model_validate(payload)
        self._documents[document_id] = record
        logger.info(
            "Loaded document record from S3",
            extra={"document_id": document_id, "version": record.version},
        )
        return record

    def list_documents(self) -> list[DocumentRecord]:
        """Return all document records ordered by title ascending.

        Records live one-per-key in S3, so the listing fans the per-document
        reads out across a bounded thread pool rather than issuing them one at a
        time (an N+1 that made the Documents tab scale linearly with the corpus).
        """
        document_ids = [
            key[len("documents/") : key.rfind("/record.json")]
            for key in self._store.list_document_record_keys()
        ]
        if not document_ids:
            logger.info("Listed 0 documents")
            return []

        max_workers = max(
            1, getattr(self._settings, "document_list_max_workers", 16)
        )
        if max_workers <= 1 or len(document_ids) == 1:
            fetched = [self.get_document(document_id) for document_id in document_ids]
        else:
            fetched = list(
                self._get_document_list_executor(max_workers).map(
                    self.get_document, document_ids
                )
            )

        records = [record for record in fetched if record is not None]
        records.sort(key=lambda r: r.title.lower())
        logger.info("Listed %d documents", len(records))
        return records

    def _get_document_list_executor(self, max_workers: int) -> ThreadPoolExecutor:
        """Return the shared Documents-listing pool, created once on first use.

        Sized to the configured maximum (not the first call's document count) so
        later, larger listings keep their full concurrency. Reused across
        ``GET /documents`` calls so the tab does not create and tear down a
        thread pool on every load. ``getattr`` keeps instances built via
        ``object.__new__`` (tests) working without ``__init__``.
        """
        executor = getattr(self, "_document_list_executor", None)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="doc-list"
            )
            self._document_list_executor = executor
        return executor

    def _save_document_record(self, record: DocumentRecord) -> None:
        key = document_record_key(record.id)
        self._persist_record(record, key)
        # Only cache after a successful persist so a rejected resurrection never
        # poisons the in-memory view with a status the store refused.
        self._documents[record.id] = record
        logger.info(
            "Persisted document record status=%s",
            record.status,
            extra={
                "document_id": record.id,
                "version": record.version,
                "s3_key": key,
            },
        )

    def _persist_record(self, record: DocumentRecord, key: str) -> None:
        """Persist a document record, refusing writes that would corrupt it.

        Two invariants are enforced against the canonical stored record:

        * ``deleted`` is terminal — a live status is rejected with
          :class:`DocumentDeletedError` when the stored record is already
          deleted, closing the last-writer-wins race where a ``DELETE`` and an
          in-flight ingestion both wrote ``record.json``.
        * the version must still match — a progress/terminal write for a version
          other than the currently stored one is rejected with
          :class:`StaleIngestionError` (a newer upload replaced it). A ``queued``
          write is exempt because that is how a fresh upload introduces the new
          version.

        When the store supports compare-and-set (real S3 via ETags) the check
        and write are atomic: we read the current ETag, verify the invariants,
        then write conditionally, retrying on conflict. Stores without CAS (the
        in-memory test doubles, which are single-threaded and never actually
        race) fall back to a read-then-write guard.
        """
        payload = record.model_dump(mode="json")
        supports_cas = hasattr(self._store, "get_json_with_etag") and hasattr(
            self._store, "put_json_conditional"
        )
        if not supports_cas:
            # No compare-and-set primitive. Best-effort read-then-write guard
            # when the store can be read; a write-only store (some minimal test
            # doubles) simply writes as before. Real S3 always takes the CAS
            # branch below, so production never relies on this fallback.
            if hasattr(self._store, "get_json"):
                self._reject_illegal_write(record, self._store.get_json(key))
            self._store.put_json(key, payload)
            return

        for _ in range(_MAX_RECORD_CAS_ATTEMPTS):
            current, etag = self._store.get_json_with_etag(key)
            self._reject_illegal_write(record, current)
            try:
                if current is None:
                    self._store.put_json_conditional(key, payload, if_none_match=True)
                else:
                    self._store.put_json_conditional(key, payload, if_match=etag)
                return
            except PreconditionFailed:
                # Another writer changed the record between our read and write.
                # Reload and re-check the invariants before retrying.
                continue
        raise PreconditionFailed(key)

    @staticmethod
    def _reject_illegal_write(record: DocumentRecord, current: object | None) -> None:
        """Reject deleted-resurrection and stale-version writes.

        See :meth:`_persist_record` for the invariants. ``queued`` and
        ``deleted`` writes are exempt from the version check: the former is how a
        fresh upload introduces a new version, the latter always carries the
        current version (delete copies the stored record).
        """
        if not isinstance(current, dict):
            # No prior record (creation) — nothing to protect.
            return

        if (
            record.status != DocumentStatus.deleted
            and current.get("status") == DocumentStatus.deleted.value
        ):
            raise DocumentDeletedError(record.id)

        current_version = current.get("version")
        if (
            current_version is not None
            and record.version != current_version
            and record.status not in (DocumentStatus.queued, DocumentStatus.deleted)
        ):
            raise StaleIngestionError(record.id, record.version, current_version)

    def get_query_trace(self, trace_id: str) -> QueryTraceRecord | None:
        payload = self._store.get_json(query_trace_key(trace_id))
        if payload is None:
            return None
        return QueryTraceRecord.model_validate(payload)

    def record_query_feedback(
        self,
        trace_id: str,
        feedback: QueryFeedbackRequest,
    ) -> QueryFeedbackRecord | None:
        if self.get_query_trace(trace_id) is None:
            return None

        record = QueryFeedbackRecord(
            trace_id=trace_id,
            feedback_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
            rating=feedback.rating,
            comment=feedback.comment,
            expected_answer=feedback.expected_answer,
        )
        self._store.put_json(
            query_feedback_key(trace_id, record.feedback_id),
            record.model_dump(mode="json"),
        )
        metrics.increment("rag_query_feedback_total", {"rating": record.rating})
        logger.info(
            "Stored query feedback",
            extra={"trace_id": trace_id, "feedback_id": record.feedback_id},
        )
        return record

    def _active_version_for(self, document_id: str) -> str | None:
        """Look up a document's published version for the search gate.

        Fail-closed: if the record is missing, deleted, or cannot be read, the
        document's vectors are treated as not-searchable (returns ``None``). In
        production every document with vectors has a record (created at queue
        time), so this only excludes genuinely orphaned/deleted vectors.
        """
        try:
            record = self.get_document(document_id)
        except Exception:
            logger.warning(
                "Active-version lookup failed; excluding document from results",
                extra={"document_id": document_id},
                exc_info=True,
            )
            return None
        return self._published_version_of(record)

    def _filter_to_active_versions(
        self, hits: list[RetrievalHit], log_extra: dict[str, Any]
    ) -> list[RetrievalHit]:
        """Keep only hits whose version matches their document's active version."""
        if not hits:
            return hits
        active_by_doc: dict[str, str | None] = {}
        kept: list[RetrievalHit] = []
        for hit in hits:
            document_id = hit.chunk.document_id
            if document_id not in active_by_doc:
                active_by_doc[document_id] = self._active_version_for(document_id)
            active_version = active_by_doc[document_id]
            if active_version is not None and hit.chunk.version == active_version:
                kept.append(hit)
        dropped = len(hits) - len(kept)
        if dropped:
            metrics.observe("rag_retrieval_non_active_hits_filtered", dropped)
            logger.info(
                "Filtered %d non-active-version hit(s) from retrieval",
                dropped,
                extra={**log_extra, "filtered_hits": dropped},
            )
        return kept

    def _retrieve(
        self,
        request: QueryRequest,
        recorder: Any,
        retrieval_mode: str,
        log_extra: dict[str, Any],
    ) -> list[RetrievalHit]:
        """Embed, retrieve, and rerank for a query; return the top hits.

        This is the retrieval half of the pipeline shared by :meth:`query` and
        :meth:`query_stream`. Keeping it in one place ensures retrieval tuning
        (embedding, sparse encoding, top-k, rerank) cannot drift between the
        batch and streaming code paths. Span recording is threaded through the
        caller's ``recorder`` so each path attributes the spans to its own trace.
        """
        with recorder.record_span("query embedding (dense)"):
            query_vector = self.embedder.embed_query(request.question)

        if self._settings.sparse_enabled:
            with recorder.record_span("query encoding (sparse/BM25)"):
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
        with recorder.record_span(retrieval_operation) as retrieval_span:
            raw_hits = self.index.search(
                query_vector=query_vector,
                sparse_vector=sparse_query,
                top_k=self._settings.retrieval_dense_top_k,
                document_ids=request.document_ids,
            )
            # Drop any hit whose version is not the document's published version.
            # This is what makes publication atomic from the reader's side:
            # in-flight/partial vectors (a not-yet-published version) and the
            # previous version's leftover vectors (awaiting cleanup) are never
            # returned, even though they physically coexist in the index.
            hits = self._filter_to_active_versions(raw_hits, log_extra)
            recorder.set_retrieval_attributes(
                retrieval_span,
                retrieval_mode=retrieval_mode,
                hit_count=len(hits),
                top_score=hits[0].score if hits else None,
            )
        logger.info(
            "Retrieved %d hits", len(hits), extra={**log_extra, "hit_count": len(hits)}
        )
        self._observe_retrieval_quality(hits, retrieval_mode, log_extra)

        # Reranking is optional — controlled by RAG_RERANK_ENABLED
        reranker = self.reranker
        if reranker:
            with recorder.record_span("reranking"):
                top_hits = reranker.rerank(request.question, hits)
            logger.info("Reranked to %d hits", len(top_hits), extra=log_extra)
        else:
            top_hits = hits[: self._settings.rerank_top_k]
        return top_hits

    def query(self, request: QueryRequest) -> QueryResponse:
        trace_id = get_trace_id() or str(uuid.uuid4())
        query_start = time.perf_counter()
        retrieval_mode = "hybrid" if self._settings.sparse_enabled else "dense"
        log_extra: dict[str, Any] = {
            "trace_id": trace_id,
            "query_len": len(request.question),
            "retrieval_mode": retrieval_mode,
        }
        logger.info("Processing query (trace=%s)", trace_id, extra=log_extra)
        metrics.increment("rag_queries_total", {"mode": retrieval_mode})
        metrics.observe("rag_query_length_chars", len(request.question), {"mode": retrieval_mode})

        from rag_system.observability_tracing import get_span_recorder

        recorder = get_span_recorder()

        top_hits = self._retrieve(request, recorder, retrieval_mode, log_extra)

        with recorder.record_span("answer generation") as answer_span:
            response = self.generator.answer(request.question, top_hits, trace_id)
            recorder.set_answer_generation_attributes(
                answer_span,
                evidence_status=response.evidence_status,
                citation_count=len(response.citations),
            )

        latency_ms = (time.perf_counter() - query_start) * 1000
        # Persist the trace off the response path — it's observability only and
        # the S3 write can add seconds the caller shouldn't have to wait for.
        self._persist_query_trace_async(
            request=request,
            response=response,
            top_hits=top_hits,
            retrieval_mode=retrieval_mode,
            latency_ms=latency_ms,
        )
        self._observe_answer_quality(response, top_hits, retrieval_mode, log_extra)
        # Record the per-request query summary (question, confidence, tokens) on
        # the trace — unless the unified router owns the summary for this request.
        if not is_unified_active():
            record_query_summary(request.question, response.confidence_score)
        logger.info("Query complete (trace=%s)", trace_id, extra=log_extra)
        return response

    def query_stream(self, request: QueryRequest) -> Iterator[dict[str, Any]]:
        """Stream a RAG answer.

        Runs the (non-streamable) retrieval pipeline first, emitting status
        events, then streams the grounded answer tokens, and finally persists
        the trace and emits the structured ``QueryResponse``.
        """
        trace_id = get_trace_id() or str(uuid.uuid4())
        query_start = time.perf_counter()
        retrieval_mode = "hybrid" if self._settings.sparse_enabled else "dense"
        log_extra: dict[str, Any] = {
            "trace_id": trace_id,
            "query_len": len(request.question),
            "retrieval_mode": retrieval_mode,
        }
        logger.info("Processing query (streaming, trace=%s)", trace_id, extra=log_extra)
        metrics.increment("rag_queries_total", {"mode": retrieval_mode})

        from rag_system.observability_tracing import get_span_recorder

        recorder = get_span_recorder()

        yield {"type": "status", "stage": "retrieving"}
        top_hits = self._retrieve(request, recorder, retrieval_mode, log_extra)

        yield {"type": "status", "stage": "generating"}
        response: QueryResponse | None = None
        for event in self.generator.answer_stream(request.question, top_hits, trace_id):
            if event.get("type") == "final":
                response = event["response"]
            else:
                yield event

        assert response is not None  # answer_stream always yields a final event
        latency_ms = (time.perf_counter() - query_start) * 1000
        self._persist_query_trace_async(
            request=request,
            response=response,
            top_hits=top_hits,
            retrieval_mode=retrieval_mode,
            latency_ms=latency_ms,
        )
        self._observe_answer_quality(response, top_hits, retrieval_mode, log_extra)
        if not is_unified_active():
            record_query_summary(request.question, response.confidence_score)
        logger.info("Query complete (streaming, trace=%s)", trace_id, extra=log_extra)
        yield {"type": "final", "response": response}

    def _persist_query_trace_async(self, **kwargs: Any) -> None:
        """Write the query trace to S3 on a bounded background pool.

        Trace persistence is best-effort observability, so a slow or failing
        S3 write never blocks or fails the user-facing query. The request's
        trace-id context is copied so background logs stay correlated. The work
        runs on a shared, size-bounded executor rather than a freshly spawned
        thread, so a burst of queries cannot create an unbounded number of
        threads.
        """
        ctx = contextvars.copy_context()

        def _run_logged() -> None:
            try:
                ctx.run(self._save_query_trace, **kwargs)
            except Exception:
                logger.warning(
                    "Background query-trace persistence failed", exc_info=True
                )

        try:
            _TRACE_PERSIST_EXECUTOR.submit(_run_logged)
        except RuntimeError:
            # Executor already shut down (e.g. during interpreter teardown) —
            # fall back to an inline best-effort write so the trace is not lost.
            _run_logged()

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

    def _save_query_trace(
        self,
        *,
        request: QueryRequest,
        response: QueryResponse,
        top_hits: list[RetrievalHit],
        retrieval_mode: str,
        latency_ms: float,
    ) -> None:
        trace = QueryTraceRecord(
            trace_id=response.trace_id,
            question=request.question,
            route="rag",
            retrieval_mode=retrieval_mode,
            document_ids=request.document_ids,
            answer=response.answer,
            evidence_status=response.evidence_status,
            confidence=response.confidence,
            confidence_score=response.confidence_score,
            insufficient_evidence_reason=response.insufficient_evidence_reason,
            citations=response.citations,
            retrieved_hits=[_trace_hit(hit) for hit in top_hits],
            model_ids=self._query_model_ids(),
            latency_ms=latency_ms,
        )
        key = query_trace_key(response.trace_id)
        self._store.put_json(key, trace.model_dump(mode="json"))
        metrics.increment("rag_query_traces_stored_total", {"route": trace.route})
        metrics.observe("rag_query_trace_latency_ms", latency_ms, {"route": trace.route})
        logger.info(
            "Stored query trace",
            extra={
                "trace_id": response.trace_id,
                "s3_key": key,
                "hit_count": len(top_hits),
                "citation_count": len(response.citations),
                "duration_ms": latency_ms,
            },
        )

    def _query_model_ids(self) -> dict[str, str]:
        model_ids = {
            "embedding": getattr(self._settings, "bedrock_embedding_model_id", None),
            "generation": getattr(self._settings, "active_llm_model_id", None),
            "pinecone_index": getattr(self._settings, "pinecone_index_name", None),
        }
        if getattr(self._settings, "sparse_enabled", False):
            model_ids["sparse"] = "bm25-msmarco-default"
        if getattr(self._settings, "rerank_enabled", False):
            model_ids["rerank"] = getattr(self._settings, "bedrock_rerank_model_id", None)
        return {key: str(value) for key, value in model_ids.items() if value}


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:24]


def _trace_hit(hit: RetrievalHit) -> QueryTraceHit:
    return QueryTraceHit(
        chunk_id=hit.chunk.id,
        document_id=hit.chunk.document_id,
        version=hit.chunk.version,
        score=hit.score,
        source=hit.source,
        text=hit.chunk.text,
        page_start=hit.chunk.page_start,
        page_end=hit.chunk.page_end,
        title=hit.chunk.metadata.get("source_filename"),
        section_path=hit.chunk.section_path,
    )
