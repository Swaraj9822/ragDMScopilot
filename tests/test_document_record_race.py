"""Regression tests for the document-record last-writer-wins race (finding 7).

A ``DELETE /documents/{id}`` and an in-flight ingestion job both wrote
``record.json`` with unconditional puts. If the delete landed mid-ingestion,
the worker's terminal ``indexed`` write could overwrite the ``deleted`` status,
resurrecting a document whose Pinecone vectors were already gone.

The fix makes ``deleted`` a terminal state and uses S3 compare-and-set (ETags)
so the check-and-write is atomic. These tests exercise that invariant through a
CAS-capable in-memory store double, including a simulated concurrent delete that
lands exactly at the worker's final write.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rag_system.models import (
    Chunk,
    DocumentRecord,
    DocumentStatus,
    EmbeddedChunk,
    ParsedDocument,
)
from rag_system.service import DocumentDeletedError, RagService, StaleIngestionError
from rag_system.queue import IngestionJob
from rag_system.storage import PreconditionFailed, document_record_key


# ---------------------------------------------------------------------------
# CAS-capable store double modelling S3 optimistic concurrency via ETags.
# ---------------------------------------------------------------------------


class CasStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        self.chunks_written = False
        #: Optional hook fired once, just before a conditional write, to
        #: simulate a concurrent writer landing between our read and write.
        #: Signature: (store, key, payload) -> None.
        self.before_conditional_write = None

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    # -- plain reads/writes -------------------------------------------------
    def get_json(self, key: str) -> object | None:
        entry = self.objects.get(key)
        return None if entry is None else entry[0]

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = (payload, self._next_etag())
        return f"s3://bucket/{key}"

    def put_chunks(self, document_id: str, version: str, chunks) -> str:
        self.chunks_written = True
        return f"s3://bucket/chunks/{document_id}/{version}/chunks.jsonl"

    def get_pdf(self, document_id: str, version: str) -> bytes:
        return b"%PDF-1.4"

    # -- compare-and-set primitives ----------------------------------------
    def get_json_with_etag(self, key: str) -> tuple[object | None, str | None]:
        entry = self.objects.get(key)
        if entry is None:
            return None, None
        return entry[0], entry[1]

    def put_json_conditional(
        self,
        key: str,
        payload: object,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        if self.before_conditional_write is not None:
            self.before_conditional_write(self, key, payload)
        entry = self.objects.get(key)
        if if_none_match:
            if entry is not None:
                raise PreconditionFailed(key)
        elif if_match is not None:
            if entry is None or entry[1] != if_match:
                raise PreconditionFailed(key)
        self.objects[key] = (payload, self._next_etag())


# ---------------------------------------------------------------------------
# Ingestion pipeline fakes (mirrors tests/test_worker.py).
# ---------------------------------------------------------------------------


class FakeParser:
    async def parse(self, document_id, version, filename, content) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            version=version,
            markdown="# Intro\nbody",
            metadata={"filename": filename},
        )


class FakeChunker:
    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        return [
            Chunk(
                id="chunk-1",
                document_id=parsed.document_id,
                version=parsed.version,
                text=parsed.markdown,
            )
        ]


class FakeEmbedder:
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [EmbeddedChunk(chunk=chunk, dense_vector=[0.1, 0.2]) for chunk in chunks]


class FakeIndex:
    def __init__(self) -> None:
        self.upserted: list[EmbeddedChunk] = []
        self.deleted_document_ids: list[str] = []

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        self.upserted.extend(embedded_chunks)

    def delete_document(self, document_id: str) -> None:
        self.deleted_document_ids.append(document_id)
        self.upserted = [
            item for item in self.upserted if item.chunk.document_id != document_id
        ]


def _record(status: DocumentStatus = DocumentStatus.queued) -> DocumentRecord:
    return DocumentRecord(
        id="doc-123",
        title="source.pdf",
        version="abc",
        s3_uri="s3://bucket/raw/doc-123/abc/source.pdf",
        status=status,
    )


def _service(store: CasStore, index: FakeIndex | None = None) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        embedding_model_id="embed-model",
        gcs_bucket="bucket",
        pinecone_index_name="index",
    )
    service._store = store
    service._documents = {}
    service._parser = FakeParser()
    service._chunker = FakeChunker()
    service._embedder = FakeEmbedder()
    service._sparse_encoder = None
    service._index = index if index is not None else FakeIndex()
    service._generator = None
    return service


# ---------------------------------------------------------------------------
# _persist_record invariant: deleted is terminal.
# ---------------------------------------------------------------------------


def test_live_status_write_over_deleted_record_is_rejected() -> None:
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.deleted).model_dump(mode="json"))
    service = _service(store)

    with pytest.raises(DocumentDeletedError):
        service._save_document_record(_record(DocumentStatus.indexed))

    # The stored record remains deleted; no resurrection.
    assert store.get_json(key)["status"] == DocumentStatus.deleted
    # And the in-memory cache was not poisoned with the rejected live status.
    assert "doc-123" not in service._documents


def test_deleting_an_already_deleted_record_is_allowed() -> None:
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.deleted).model_dump(mode="json"))
    service = _service(store)

    # Writing the deleted status again (idempotent delete) must not raise.
    service._save_document_record(_record(DocumentStatus.deleted))
    assert store.get_json(key)["status"] == DocumentStatus.deleted


def test_cas_conflict_retries_then_succeeds() -> None:
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.queued).model_dump(mode="json"))
    service = _service(store)

    def concurrent_nonconflicting_write(s: CasStore, k: str, _payload) -> None:
        # Another (non-deleting) writer bumps the record's ETag first, so our
        # first conditional write loses the race and must reload + retry. Clear
        # the hook so the retry succeeds.
        s.before_conditional_write = None
        s.objects[k] = (_record(DocumentStatus.parsing).model_dump(mode="json"), s._next_etag())

    store.before_conditional_write = concurrent_nonconflicting_write

    service._save_document_record(_record(DocumentStatus.embedding))

    assert store.get_json(key)["status"] == DocumentStatus.embedding


def test_sustained_conflict_surfaces_as_precondition_failed() -> None:
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.queued).model_dump(mode="json"))
    service = _service(store)

    # A writer that bumps the ETag on *every* attempt so CAS never converges.
    def always_conflict(s: CasStore, k: str, payload) -> None:
        s.objects[k] = (s.objects[k][0], s._next_etag())
        s.before_conditional_write = always_conflict

    store.before_conditional_write = always_conflict

    with pytest.raises(PreconditionFailed):
        service._save_document_record(_record(DocumentStatus.embedding))


# ---------------------------------------------------------------------------
# End-to-end: a concurrent delete during ingestion.
# ---------------------------------------------------------------------------


def test_concurrent_delete_during_ingestion_rolls_back_and_does_not_index() -> None:
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.queued).model_dump(mode="json"))
    index = FakeIndex()
    service = _service(store, index)

    def delete_lands_at_indexed_write(s: CasStore, k: str, payload) -> None:
        # A DELETE lands exactly as the worker tries to write the terminal
        # ``indexed`` status — after vectors were already upserted.
        if isinstance(payload, dict) and payload.get("status") == DocumentStatus.indexed:
            deleted = {**payload, "status": DocumentStatus.deleted.value, "error": None}
            s.objects[k] = (deleted, s._next_etag())

    store.before_conditional_write = delete_lands_at_indexed_write

    result = asyncio.run(service._run_ingestion(_record(), b"%PDF-1.4"))

    # The document ends up deleted, not indexed.
    assert result.status == DocumentStatus.deleted
    assert store.get_json(key)["status"] == DocumentStatus.deleted
    # Vectors upserted mid-flight were rolled back.
    assert index.deleted_document_ids == ["doc-123"]
    assert index.upserted == []


# ---------------------------------------------------------------------------
# _persist_record invariant: version must match (stale-write protection).
# ---------------------------------------------------------------------------


def test_stale_progress_write_over_newer_version_is_rejected() -> None:
    # A newer upload (version "v2") already replaced the record. A stale
    # ingestion for the old version must not clobber it with its progress write.
    store = CasStore()
    key = document_record_key("doc-123")
    newer = _record(DocumentStatus.queued).model_copy(update={"version": "v2"})
    store.put_json(key, newer.model_dump(mode="json"))
    service = _service(store)

    with pytest.raises(StaleIngestionError):
        service._save_document_record(_record(DocumentStatus.indexed))  # version "abc"

    # The newer queued record is preserved; the cache is not poisoned.
    assert store.get_json(key)["version"] == "v2"
    assert store.get_json(key)["status"] == DocumentStatus.queued
    assert "doc-123" not in service._documents


def test_new_upload_queued_write_replaces_in_flight_version() -> None:
    # A fresh upload legitimately introduces a new version, so a ``queued`` write
    # is exempt from the version guard and replaces the in-flight record.
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.parsing).model_dump(mode="json"))
    service = _service(store)

    replacement = _record(DocumentStatus.queued).model_copy(update={"version": "v2"})
    service._save_document_record(replacement)

    assert store.get_json(key)["version"] == "v2"
    assert store.get_json(key)["status"] == DocumentStatus.queued


def test_process_document_job_drops_stale_version_as_no_op() -> None:
    # When the stored record has moved on to a newer version, the old job is
    # stale: it raises StaleIngestionError (which the worker drops as a no-op)
    # instead of a bare ValueError that would retry to the DLQ.
    store = CasStore()
    key = document_record_key("doc-123")
    current = _record(DocumentStatus.queued).model_copy(update={"version": "v2"})
    store.put_json(key, current.model_dump(mode="json"))
    service = _service(store)

    job = IngestionJob(
        document_id="doc-123",
        version="abc",  # superseded by "v2"
        filename="source.pdf",
        s3_uri="s3://bucket/raw/doc-123/abc/source.pdf",
    )

    with pytest.raises(StaleIngestionError):
        asyncio.run(service.process_document_job(job))


def test_ingestion_superseded_midflight_stops_without_overwrite_or_rollback() -> None:
    # A newer upload lands exactly as the worker writes the terminal ``indexed``
    # status. The stale write must be refused, the newer record preserved, and
    # vectors must NOT be deleted (the newer version owns the document).
    store = CasStore()
    key = document_record_key("doc-123")
    store.put_json(key, _record(DocumentStatus.queued).model_dump(mode="json"))
    index = FakeIndex()
    service = _service(store, index)

    def newer_upload_lands_at_indexed_write(s: CasStore, k: str, payload) -> None:
        if isinstance(payload, dict) and payload.get("status") == DocumentStatus.indexed:
            newer = {
                **payload,
                "version": "newer-version",
                "status": DocumentStatus.queued.value,
                "error": None,
            }
            s.objects[k] = (newer, s._next_etag())

    store.before_conditional_write = newer_upload_lands_at_indexed_write

    result = asyncio.run(service._run_ingestion(_record(), b"%PDF-1.4"))

    # The newer version's queued record survives; the stale "indexed" write lost.
    assert store.get_json(key)["version"] == "newer-version"
    assert store.get_json(key)["status"] == DocumentStatus.queued
    assert result.version == "newer-version"
    # Vectors upserted mid-flight are left in place, not rolled back.
    assert index.deleted_document_ids == []
    assert index.upserted != []
