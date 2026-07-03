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
from rag_system.embedding import GeminiEmbedder
from rag_system.generation import GroundedAnswerGenerator
from rag_system.models import (
    BenchmarkCase,
    DocumentHistory,
    DocumentRecord,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionIndex,
    FeedbackReviewRecord,
    IngestionEvent,
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
from rag_system.queue import IngestionJob, PubSubIngestionQueue
from rag_system.rerank import BedrockCohereReranker
from rag_system.retrieval import PineconeHybridIndex
from rag_system.sparse import BM25SparseEncoder
from rag_system.storage import (
    PreconditionFailed,
    GcsArtifactStore,
    chunks_key,
    document_record_key,
    document_version_index_key,
    document_version_key,
    embedding_manifest_key,
    evaluation_run_results_key,
    evaluation_set_case_key,
    ingestion_event_key,
    parsed_key,
    query_feedback_key,
    query_trace_key,
)
from rag_system.feedback import (
    AlreadyInEvaluationSetError,
    classify_feedback_record,
    parse_failure_category,
    promote_feedback_record,
    resolve_feedback_record,
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


class DocumentVersionNotFoundError(RuntimeError):
    """Raised when a restore targets a version the Document does not have.

    Signals R5.10: the requested ``Document_Version`` does not exist for the
    Document, so the caller must leave the ``Active_Version`` unchanged and
    surface a ``version_not_found`` error to the operator.
    """

    def __init__(self, document_id: str, version: str) -> None:
        super().__init__(
            f"Document {document_id} has no version {version} to restore"
        )
        self.document_id = document_id
        self.version = version


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
        self._store = GcsArtifactStore(settings)
        self._queue = PubSubIngestionQueue(settings)
        self._parser: DocumentParserRouter | None = None
        self._chunker: DocumentChunker | None = None
        self._embedder: GeminiEmbedder | None = None
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
    def queue(self) -> PubSubIngestionQueue:
        return self._queue

    @property
    def artifact_store(self) -> GcsArtifactStore:
        """The shared GCS artifact store (documents, traces, conversations)."""
        return self._store

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
    def embedder(self) -> GeminiEmbedder:
        if self._embedder is None:
            self._embedder = GeminiEmbedder(self._settings)
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
                    "embedding_model": self._settings.embedding_model_id,
                    "sparse_model": "bm25-msmarco-default" if self._settings.sparse_enabled else None,
                    "pinecone_index": self._settings.pinecone_index_name,
                    "chunks_uri": (
                        f"gs://{self._settings.gcs_bucket}/"
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
            # R5.1/R5.2/R5.4/R5.5: formalize the successful ingestion into
            # first-class version control artifacts — create the immutable
            # Document_Version manifest, record a succeeded Ingestion_Event, and
            # publish the version as active through the version-index CAS write
            # (which enforces at-most-one active version). Source content of
            # every version is retained (never deleted), satisfying R5.5.
            await asyncio.to_thread(
                self._record_ingestion_success, document_id, version, log_extra
            )
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
            # R5.3: a failed ingestion creates NO new Document_Version and leaves
            # the current Active_Version untouched (neither the manifest nor the
            # version-index active pointer is written here), but it MUST record a
            # failed Ingestion_Event so the failure appears in the document's
            # ingestion history.
            await asyncio.to_thread(
                self._record_ingestion_failure,
                document_id,
                version,
                str(exc),
                log_extra,
            )
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

    # --- Document version control (R5.1-R5.5) -------------------------------

    def _record_ingestion_success(
        self, document_id: str, version: str, log_extra: dict[str, Any]
    ) -> None:
        """Persist the version-control artifacts for a successful ingestion.

        Creates the immutable ``Document_Version`` manifest (R5.1), records a
        succeeded ``Ingestion_Event`` (R5.1), and publishes the version as the
        document's ``Active_Version`` through a compare-and-set write to the
        version index (R5.2), which enforces at-most-one active version (R5.4).
        The source content of every version is retained — no version content is
        ever deleted — so R5.5 holds. The manifest write is create-only and
        therefore idempotent: re-ingesting identical content (the same
        content-hash version) reuses the existing manifest instead of failing.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        manifest = DocumentVersion(
            document_id=document_id,
            version=version,
            created_at=timestamp,
            indexed=True,
            vectors_present=True,
            source_retained=True,
        )
        # 1. Immutable per-version manifest (create-only; idempotent per version).
        self._create_artifact(
            document_version_key(document_id, version),
            manifest.model_dump(mode="json"),
        )
        # 2. Succeeded ingestion event (always a distinct history entry).
        self._record_ingestion_event(document_id, version, "succeeded", timestamp, None)
        # 3. Publish as active via the version-index CAS write.
        self._activate_version_in_index(document_id, manifest)
        logger.info(
            "Recorded successful Document_Version and Ingestion_Event",
            extra=log_extra,
        )

    def _record_ingestion_failure(
        self,
        document_id: str,
        version: str,
        error: str,
        log_extra: dict[str, Any],
    ) -> None:
        """Record only a failed ``Ingestion_Event`` for a failed ingestion (R5.3).

        No ``Document_Version`` manifest is created and the version index is not
        touched, so the current ``Active_Version`` is left unchanged.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        self._record_ingestion_event(document_id, version, "failed", timestamp, error)
        logger.info(
            "Recorded failed Ingestion_Event (no version created)",
            extra=log_extra,
        )

    def _record_ingestion_event(
        self,
        document_id: str,
        version: str,
        status: str,
        timestamp: str,
        error: str | None,
    ) -> None:
        """Persist a single immutable ``Ingestion_Event`` (create-only)."""
        event = IngestionEvent(
            ingestion_id=str(uuid.uuid4()),
            document_id=document_id,
            version=version,
            status=status,
            timestamp=timestamp,
            error=error,
        )
        self._create_artifact(
            ingestion_event_key(document_id, event.ingestion_id),
            event.model_dump(mode="json"),
        )

    def _activate_version_in_index(
        self, document_id: str, manifest: DocumentVersion
    ) -> None:
        """Add ``manifest`` to the version index and mark it active (R5.2/R5.4).

        The index is the ordered list of a document's versions plus a single
        active pointer. The compare-and-set write guarantees concurrent
        ingestions cannot both win the active pointer, so at most one active
        version ever holds (R5.4). Prior version entries are retained (R5.5/R5.11).
        """

        def mutate(current: object | None) -> object:
            if isinstance(current, dict):
                index = DocumentVersionIndex.model_validate(current)
            else:
                index = DocumentVersionIndex(document_id=document_id)
            # Replace any existing entry for this version, then append the fresh
            # manifest so re-ingestion updates rather than duplicates it.
            index.versions = [
                v for v in index.versions if v.version != manifest.version
            ]
            # Publishing a new active version triggers cleanup of every other
            # version's vectors (see _cleanup_superseded_vectors), so record in
            # the index that the superseded versions no longer hold live vectors.
            # The restore path reads this flag to decide whether it can flip the
            # active pointer directly (R5.8) or must re-index from the retained
            # source first (R5.9). The immutable per-version manifest is left
            # untouched; the index is the mutable source of truth for liveness.
            for other in index.versions:
                other.vectors_present = False
            index.versions.append(manifest)
            index.active_version = manifest.version
            return index.model_dump(mode="json")

        self._cas_update(document_version_index_key(document_id), mutate)

    # --- Document version history & restore (R5.6-R5.11) --------------------

    def _load_version_index(self, document_id: str) -> DocumentVersionIndex | None:
        """Load a Document's version index, or ``None`` when none exists yet."""
        if not hasattr(self._store, "get_json"):
            return None
        payload = self._store.get_json(document_version_index_key(document_id))
        if payload is None:
            return None
        return DocumentVersionIndex.model_validate(payload)

    def _load_ingestion_events(self, document_id: str) -> list[IngestionEvent]:
        """Load every retained Ingestion_Event for a Document (unordered)."""
        if not hasattr(self._store, "list_ingestion_event_keys") or not hasattr(
            self._store, "get_json"
        ):
            return []
        events: list[IngestionEvent] = []
        for key in self._store.list_ingestion_event_keys(document_id):
            payload = self._store.get_json(key)
            if payload is not None:
                events.append(IngestionEvent.model_validate(payload))
        return events

    def get_document_history(self, document_id: str) -> DocumentHistory | None:
        """Return a Document's versions + ingestion events, newest first (R5.7).

        Returns ``None`` when the Document does not exist or has been deleted so
        the API layer can surface a 404. Versions are ordered by their creation
        timestamp and events by their ingestion timestamp, most recent first.
        """
        record = self.get_document(document_id)
        if record is None or record.status == DocumentStatus.deleted:
            return None

        index = self._load_version_index(document_id)
        versions = list(index.versions) if index is not None else []
        active_version = (
            index.active_version
            if index is not None
            else self._published_version_of(record)
        )
        events = self._load_ingestion_events(document_id)

        # ISO-8601 UTC timestamps sort lexicographically, so a reverse string
        # sort yields most-recent-first without parsing.
        versions.sort(key=lambda v: v.created_at, reverse=True)
        events.sort(key=lambda e: e.timestamp, reverse=True)

        return DocumentHistory(
            document_id=document_id,
            active_version=active_version,
            versions=versions,
            events=events,
        )

    def restore_version(self, document_id: str, version: str) -> DocumentRecord | None:
        """Restore a previous Document_Version as the Active_Version (R5.8-R5.11).

        Returns ``None`` when the Document does not exist or is deleted. Raises
        :class:`DocumentVersionNotFoundError` when the target version is unknown
        for the Document, leaving the Active_Version unchanged (R5.10). When the
        target version's vectors still exist, the active pointer is flipped
        directly (R5.8); when they were cleaned up, the version is re-indexed
        from its retained source content first (R5.9). All prior versions are
        retained (R5.11), and retrieval uses the newly active version (R5.6).
        """
        record = self.get_document(document_id)
        if record is None or record.status == DocumentStatus.deleted:
            return None

        index = self._load_version_index(document_id)
        target = None
        if index is not None:
            target = next(
                (v for v in index.versions if v.version == version), None
            )
        if target is None:
            # R5.10: unknown version — leave the active version untouched.
            raise DocumentVersionNotFoundError(document_id, version)

        log_extra: dict[str, Any] = {"document_id": document_id, "version": version}
        if not target.vectors_present:
            # R5.9: the version's vectors were cleaned up after being superseded;
            # rebuild them from the retained source content before activating.
            logger.info(
                "Restoring version with cleaned-up vectors; re-indexing from "
                "retained source",
                extra=log_extra,
            )
            self._reindex_version(document_id, version)
        else:
            logger.info(
                "Restoring version with existing vectors; flipping active pointer",
                extra=log_extra,
            )

        # Publish the target as active in the version index (R5.8/R5.11): the
        # target now holds live vectors; every other version is marked as not
        # holding live vectors, and all version entries are retained.
        self._activate_existing_version(document_id, version)

        # Point the Document record's active version at the restored version so
        # the retrieval search-gate serves it (R5.6). The record's own upload
        # ``version`` is left unchanged (it tracks the latest upload, not the
        # active pointer), so this write passes the stale-version guard.
        restored = record.model_copy(
            update={
                "active_version": version,
                "status": DocumentStatus.indexed,
                "error": None,
            }
        )
        self._save_document_record(restored)
        metrics.increment("rag_document_versions_restored_total")
        logger.info("Restored Document_Version as active", extra=log_extra)
        return restored

    def _activate_existing_version(self, document_id: str, version: str) -> None:
        """Flip the version index's active pointer to an existing version.

        Marks the target version as holding live vectors and every other version
        as not, then sets the active pointer to the target. All version entries
        are retained (R5.11). Uses the same CAS write as ingestion so concurrent
        publishes cannot corrupt the active pointer.
        """

        def mutate(current: object | None) -> object:
            if not isinstance(current, dict):
                raise DocumentVersionNotFoundError(document_id, version)
            index = DocumentVersionIndex.model_validate(current)
            if not any(v.version == version for v in index.versions):
                raise DocumentVersionNotFoundError(document_id, version)
            for v in index.versions:
                v.vectors_present = v.version == version
            index.active_version = version
            return index.model_dump(mode="json")

        self._cas_update(document_version_index_key(document_id), mutate)

    def _reindex_version(self, document_id: str, version: str) -> None:
        """Re-index a Document_Version from its retained content (R5.9).

        Reads the version's retained chunks, re-embeds them (adding sparse
        vectors when hybrid retrieval is enabled), and upserts them so the
        version's vectors exist again before it is activated.
        """
        if not hasattr(self._store, "get_chunks"):
            raise RuntimeError(
                "Store cannot re-index a restored version: retained chunks are "
                "unavailable"
            )
        chunks = self._store.get_chunks(document_id, version)
        if not chunks:
            raise RuntimeError(
                f"No retained content found to re-index version {version} of "
                f"document {document_id}"
            )
        embedded = self.embedder.embed_chunks(chunks)
        if self._settings.sparse_enabled:
            sparse_vectors = self.sparse_encoder.encode_documents(
                [c.text for c in chunks]
            )
            for ec, sv in zip(embedded, sparse_vectors, strict=True):
                ec.sparse_vector = sv
        self.index.upsert(embedded)

    def _create_artifact(self, key: str, payload: object) -> None:
        """Create-only write of an immutable artifact.

        Uses the store's create-only primitive when available (real S3 and
        CAS-capable doubles), degrading to a plain ``put_json`` for the minimal
        write-only doubles used in some tests. A :class:`PreconditionFailed`
        (the key already exists) is swallowed so create-only writes are
        idempotent for content that hashes to an already-recorded version.
        """
        try:
            if hasattr(self._store, "create_json"):
                self._store.create_json(key, payload)
            elif hasattr(self._store, "put_json_conditional"):
                self._store.put_json_conditional(key, payload, if_none_match=True)
            else:
                self._store.put_json(key, payload)
        except PreconditionFailed:
            # The artifact already exists — create-only writes are immutable, so
            # treat a second write as a no-op rather than an error.
            pass

    def _cas_update(self, key: str, mutate: Any) -> None:
        """Read-modify-write a JSON artifact under optimistic concurrency.

        Prefers the store's native CAS helper, falls back to a manual
        get-etag/put-conditional loop for doubles exposing those primitives, and
        finally degrades to a read-then-write for minimal write-only doubles
        (single-threaded tests that never actually race).
        """
        if hasattr(self._store, "update_json_cas"):
            self._store.update_json_cas(key, mutate)
            return
        if hasattr(self._store, "get_json_with_etag") and hasattr(
            self._store, "put_json_conditional"
        ):
            for _ in range(_MAX_RECORD_CAS_ATTEMPTS):
                current, etag = self._store.get_json_with_etag(key)
                payload = mutate(current)
                try:
                    if current is None:
                        self._store.put_json_conditional(
                            key, payload, if_none_match=True
                        )
                    else:
                        self._store.put_json_conditional(key, payload, if_match=etag)
                    return
                except PreconditionFailed:
                    continue
            raise PreconditionFailed(key)
        current = (
            self._store.get_json(key) if hasattr(self._store, "get_json") else None
        )
        self._store.put_json(key, mutate(current))

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

    # ------------------------------------------------------------------
    # Feedback review inbox actions (R6.5-R6.11)
    #
    # Actions are addressed by ``feedback_id`` alone (the API path is
    # ``/feedback/{id}/...``). Because a feedback record lives under its
    # trace's prefix, the full storage key is resolved by scanning the feedback
    # keys, then mutated under ETag-CAS so concurrent operators never clobber
    # one another's classification/resolution.
    # ------------------------------------------------------------------

    def _feedback_key_for(self, feedback_id: str) -> str | None:
        """Resolve the full S3 key for a feedback record by its id, or ``None``."""
        suffix = f"/feedback/{feedback_id}.json"
        for key in self._store.list_feedback_record_keys():
            if key.endswith(suffix):
                return key
        return None

    def get_feedback_review_record(
        self, feedback_id: str
    ) -> FeedbackReviewRecord | None:
        """Load a single feedback record as a :class:`FeedbackReviewRecord`.

        A legacy :class:`QueryFeedbackRecord` payload (written before the review
        fields existed) validates cleanly, defaulting to ``unreviewed`` with no
        category — so the inbox actions work uniformly across old and new records.
        """
        key = self._feedback_key_for(feedback_id)
        if key is None:
            return None
        payload = self._store.get_json(key)
        if payload is None:
            return None
        return FeedbackReviewRecord.model_validate(payload)

    def list_feedback_reviews(self) -> list[FeedbackReviewRecord]:
        """Load every persisted feedback record as a :class:`FeedbackReviewRecord`.

        Backs the operator-only feedback inbox (``GET /feedback``). Like
        :meth:`list_documents`, the per-record reads (one JSON blob per key) are
        fanned out across a bounded thread pool rather than issued serially, so
        the inbox does not scale linearly in round trips with the feedback
        volume. Records that fail validation (corrupt/partial writes) are skipped
        rather than failing the whole listing.
        """
        keys = self._store.list_feedback_record_keys()
        if not keys:
            return []

        max_workers = max(1, getattr(self._settings, "document_list_max_workers", 16))
        if max_workers <= 1 or len(keys) == 1:
            payloads = [self._store.get_json(key) for key in keys]
        else:
            payloads = list(
                self._get_document_list_executor(max_workers).map(
                    self._store.get_json, keys
                )
            )

        records: list[FeedbackReviewRecord] = []
        for key, payload in zip(keys, payloads):
            if payload is None:
                continue
            try:
                records.append(FeedbackReviewRecord.model_validate(payload))
            except Exception:  # noqa: BLE001 - skip a corrupt record, not the page
                logger.warning("Skipping invalid feedback record at key %s", key)
        return records

    # ------------------------------------------------------------------
    # Multi-method evaluation runs (R7)
    # ------------------------------------------------------------------

    def list_benchmark_cases(self):
        """Load every Benchmark_Case in the default Evaluation_Set (R7)."""
        from rag_system.models import BenchmarkCase

        set_id = self._settings.default_evaluation_set_id
        cases: list[BenchmarkCase] = []
        for key in self._store.list_evaluation_set_case_keys(set_id):
            payload = self._store.get_json(key)
            if payload is None:
                continue
            try:
                cases.append(BenchmarkCase.model_validate(payload))
            except Exception:  # noqa: BLE001 - skip a corrupt case, not the run
                logger.warning("Skipping invalid benchmark case at key %s", key)
        return cases

    def run_evaluation(self):
        """Execute a deterministic evaluation run over the default set (R7.1–R7.6).

        Runs every Benchmark_Case through the RAG pipeline, scores the
        deterministic checks (citation presence, required facts, evidence
        status), and — when a case carries relevance labels — computes retrieval
        metrics against the query trace's retrieved hits (R7.2, R7.9). The CI
        pass/fail is decided solely by the deterministic checks (R7.5, R7.6);
        LLM-judge scoring is a separate scheduled report and is not part of this
        run. The run is persisted create-only and returned.

        Raises :class:`~rag_system.evaluation.EvaluationSetValidationError` when
        the set has no human-reviewed case (R7.4).
        """
        from rag_system.evaluation import (
            ci_run_passed,
            evaluate_benchmark_case,
            validate_evaluation_set,
        )
        from rag_system.models import (
            EvaluationRunDetail,
            QueryRequest,
        )
        from rag_system.retrieval_metrics import compute_retrieval_metrics

        cases = self.list_benchmark_cases()
        validate_evaluation_set(cases)  # R7.4 — raises when no human-reviewed case

        depth = getattr(self._settings, "retrieval_metric_depth_k", 10)
        results = []
        for case in cases:
            response = self.query(
                QueryRequest(question=case.question, document_ids=case.document_ids)
            )
            result = evaluate_benchmark_case(case, response)
            # R7.2 — retrieval metrics only when the case carries relevance
            # labels and the trace's retrieved hits are available.
            if case.relevance_labels is not None:
                trace = self.get_query_trace(response.trace_id)
                if trace is not None:
                    result = result.model_copy(
                        update={
                            "retrieval_metrics": compute_retrieval_metrics(
                                trace.retrieved_hits, case.relevance_labels, depth
                            )
                        }
                    )
            results.append(result)

        run_id = str(uuid.uuid4())
        detail = EvaluationRunDetail(
            run_id=run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            ci_passed=ci_run_passed(results),
            results=results,
        )
        self._store.create_json(
            evaluation_run_results_key(run_id), detail.model_dump(mode="json")
        )
        logger.info(
            "Recorded evaluation run",
            extra={
                "run_id": run_id,
                "case_count": len(results),
                "ci_passed": detail.ci_passed,
            },
        )
        metrics.increment(
            "rag_evaluation_runs_total",
            {"ci_passed": str(detail.ci_passed).lower()},
        )
        return detail

    def list_evaluation_runs(self):
        """Return summaries of all persisted evaluation runs, newest first (R7.7)."""
        from rag_system.models import EvaluationRunDetail, EvaluationRunSummary

        summaries: list[EvaluationRunSummary] = []
        for key in self._store.list_evaluation_run_keys():
            payload = self._store.get_json(key)
            if payload is None:
                continue
            try:
                detail = EvaluationRunDetail.model_validate(payload)
            except Exception:  # noqa: BLE001 - skip a corrupt run, not the listing
                logger.warning("Skipping invalid evaluation run at key %s", key)
                continue
            summaries.append(
                EvaluationRunSummary(
                    run_id=detail.run_id,
                    created_at=detail.created_at,
                    ci_passed=detail.ci_passed,
                    result_count=len(detail.results),
                )
            )
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return summaries

    def get_evaluation_run(self, run_id: str):
        """Return the full detail of a persisted evaluation run, or ``None`` (R7.7)."""
        from rag_system.models import EvaluationRunDetail

        payload = self._store.get_json(evaluation_run_results_key(run_id))
        if payload is None:
            return None
        return EvaluationRunDetail.model_validate(payload)


    def classify_feedback(
        self, feedback_id: str, category: str, reviewer: str
    ) -> FeedbackReviewRecord | None:
        """Classify a Feedback_Item with a Failure_Category (R6.5, R6.10).

        Returns ``None`` when no such feedback exists. Raises
        :class:`~rag_system.feedback.InvalidFailureCategoryError` for an
        out-of-vocabulary category, leaving the stored record untouched.
        """
        key = self._feedback_key_for(feedback_id)
        if key is None:
            return None

        # Validate up front so an invalid category never triggers a storage
        # write (R6.10: stored category left unchanged).
        parse_failure_category(category)
        reviewed_at = datetime.now(timezone.utc).isoformat()
        holder: dict[str, FeedbackReviewRecord] = {}

        def mutate(current: object | None) -> object:
            record = FeedbackReviewRecord.model_validate(current)
            updated = classify_feedback_record(
                record,
                category=category,
                reviewer=reviewer,
                reviewed_at=reviewed_at,
            )
            holder["record"] = updated
            return updated.model_dump(mode="json")

        self._cas_update(key, mutate)
        logger.info(
            "Classified feedback",
            extra={"feedback_id": feedback_id, "category": str(category)},
        )
        return holder["record"]

    def resolve_feedback(self, feedback_id: str) -> FeedbackReviewRecord | None:
        """Mark a Feedback_Item as resolved, keeping it in the inbox (R6.8)."""
        key = self._feedback_key_for(feedback_id)
        if key is None:
            return None

        holder: dict[str, FeedbackReviewRecord] = {}

        def mutate(current: object | None) -> object:
            record = FeedbackReviewRecord.model_validate(current)
            updated = resolve_feedback_record(record)
            holder["record"] = updated
            return updated.model_dump(mode="json")

        self._cas_update(key, mutate)
        logger.info("Resolved feedback", extra={"feedback_id": feedback_id})
        return holder["record"]

    def promote_feedback(self, feedback_id: str) -> BenchmarkCase | None:
        """Promote a reviewed Feedback_Item into the Evaluation_Set (R6.6-R6.11).

        Returns ``None`` when no such feedback exists. Raises
        :class:`~rag_system.feedback.ExpectedAnswerRequiredError` (R6.7) or
        :class:`~rag_system.feedback.AlreadyInEvaluationSetError` (R6.11) when the
        promotion is not permitted; in neither case is a Benchmark_Case created.
        """
        key = self._feedback_key_for(feedback_id)
        if key is None:
            return None

        record = self.get_feedback_review_record(feedback_id)
        if record is None:
            return None

        # The question is read from the joined trace (empty when the trace has
        # expired). Guards (already-promoted / expected-answer-required) run
        # inside promote_feedback_record.
        trace = self.get_query_trace(record.trace_id)
        question = trace.question if trace is not None else ""
        _, case = promote_feedback_record(record, question=question)

        set_id = self._settings.default_evaluation_set_id
        case_key = evaluation_set_case_key(set_id, case.id)

        # Create the Benchmark_Case immutably. A concurrent double-promote loses
        # the create-only race and is reported as already-in-set (R6.11).
        try:
            if hasattr(self._store, "create_json"):
                self._store.create_json(case_key, case.model_dump(mode="json"))
            elif hasattr(self._store, "put_json_conditional"):
                self._store.put_json_conditional(
                    case_key, case.model_dump(mode="json"), if_none_match=True
                )
            else:  # minimal write-only doubles
                self._store.put_json(case_key, case.model_dump(mode="json"))
        except PreconditionFailed as exc:
            raise AlreadyInEvaluationSetError(
                "Feedback_Item is already present in the Evaluation_Set."
            ) from exc

        # Record the de-dup pointer on the feedback record under CAS, re-checking
        # the guard against a racing writer.
        def mutate(current: object | None) -> object:
            current_record = FeedbackReviewRecord.model_validate(current)
            updated, _ = promote_feedback_record(current_record, question=question)
            return updated.model_dump(mode="json")

        self._cas_update(key, mutate)
        logger.info(
            "Promoted feedback to evaluation set",
            extra={
                "feedback_id": feedback_id,
                "case_id": case.id,
                "evaluation_set_id": set_id,
            },
        )
        return case

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
            "embedding": getattr(self._settings, "embedding_model_id", None),
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
