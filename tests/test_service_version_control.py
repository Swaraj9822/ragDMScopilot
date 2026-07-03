"""Tests for document version-control formalization on ingestion (R5.1-R5.5).

These exercise ``RagService._run_ingestion`` and assert that a successful
ingestion creates a ``DocumentVersion`` manifest, records a succeeded
``IngestionEvent``, and publishes the version as active via the version-index
CAS write; while a failed ingestion creates no version, leaves the active
pointer unchanged, and records a failed ``IngestionEvent``.

A CAS-capable store double is used so the version-index compare-and-set path is
genuinely exercised, mirroring ``test_index_publication.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rag_system.models import (
    Chunk,
    DocumentRecord,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionIndex,
    EmbeddedChunk,
    IngestionEvent,
    ParsedDocument,
)
from rag_system.service import RagService
from rag_system.storage import (
    PreconditionFailed,
    document_record_key,
    document_version_index_key,
    document_version_key,
)


# ---------------------------------------------------------------------------
# CAS-capable store double (mirrors test_index_publication.CasStore).
# ---------------------------------------------------------------------------


class CasStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        #: Optional predicate (key, payload) -> bool; when it returns True the
        #: put is rejected to simulate a mid-pipeline storage failure.
        self.fail_put_json_when = None

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    def get_json(self, key: str) -> object | None:
        entry = self.objects.get(key)
        return None if entry is None else entry[0]

    def put_json(self, key: str, payload: object) -> str:
        if self.fail_put_json_when is not None and self.fail_put_json_when(key, payload):
            raise RuntimeError("simulated storage failure")
        self.objects[key] = (payload, self._next_etag())
        return f"s3://bucket/{key}"

    def put_raw(self, document_id: str, version: str, filename: str, content: bytes) -> str:
        key = f"raw/{document_id}/{version}/{filename}"
        self.objects[key] = (content, self._next_etag())
        return f"s3://bucket/{key}"

    def put_chunks(self, document_id: str, version: str, chunks) -> str:
        return f"s3://bucket/chunks/{document_id}/{version}/chunks.jsonl"

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
        entry = self.objects.get(key)
        if if_none_match:
            if entry is not None:
                raise PreconditionFailed(key)
        elif if_match is not None:
            if entry is None or entry[1] != if_match:
                raise PreconditionFailed(key)
        self.objects[key] = (payload, self._next_etag())


class VersionedFakeIndex:
    def __init__(self) -> None:
        self.vectors: list[EmbeddedChunk] = []

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        self.vectors.extend(embedded_chunks)

    def delete_document(self, document_id: str) -> None:
        self.vectors = [v for v in self.vectors if v.chunk.document_id != document_id]

    def delete_document_version(self, document_id: str, version: str) -> None:
        self.vectors = [
            v
            for v in self.vectors
            if not (v.chunk.document_id == document_id and v.chunk.version == version)
        ]

    def delete_document_except_version(self, document_id: str, keep_version: str) -> None:
        self.vectors = [
            v
            for v in self.vectors
            if not (v.chunk.document_id == document_id and v.chunk.version != keep_version)
        ]


class FakeParser:
    async def parse(self, document_id, version, filename, content) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id, version=version, markdown="body", metadata={}
        )


class FakeChunker:
    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        return [
            Chunk(
                id=f"{parsed.document_id}:{parsed.version}:0",
                document_id=parsed.document_id,
                version=parsed.version,
                text=parsed.markdown,
            )
        ]


class FakeEmbedder:
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [EmbeddedChunk(chunk=chunk, dense_vector=[0.1, 0.2]) for chunk in chunks]


def _service(store: CasStore, index: VersionedFakeIndex) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        embedding_model_id="embed-model",
        gcs_bucket="bucket",
        pinecone_index_name="index",
    )
    service._store = store
    service._queue = None
    service._documents = {}
    service._parser = FakeParser()
    service._chunker = FakeChunker()
    service._embedder = FakeEmbedder()
    service._sparse_encoder = None
    service._index = index
    service._reranker = None
    service._generator = None
    return service


def _record(
    version: str, status: DocumentStatus, active_version: str | None = None
) -> DocumentRecord:
    return DocumentRecord(
        id="doc-1",
        title="source.pdf",
        version=version,
        s3_uri=f"s3://bucket/raw/doc-1/{version}/source.pdf",
        status=status,
        active_version=active_version,
    )


def _events(store: CasStore) -> list[IngestionEvent]:
    prefix = "documents/doc-1/ingestions/"
    return [
        IngestionEvent.model_validate(payload)
        for key, (payload, _etag) in store.objects.items()
        if key.startswith(prefix)
    ]


def _index_of(store: CasStore) -> DocumentVersionIndex:
    payload = store.get_json(document_version_index_key("doc-1"))
    assert payload is not None
    return DocumentVersionIndex.model_validate(payload)


# ---------------------------------------------------------------------------
# R5.1, R5.2, R5.4, R5.5 — successful ingestion.
# ---------------------------------------------------------------------------


def test_successful_ingestion_creates_version_event_and_activates() -> None:
    store = CasStore()
    store.put_json(
        document_record_key("doc-1"),
        _record("v1", DocumentStatus.queued).model_dump(mode="json"),
    )
    service = _service(store, VersionedFakeIndex())

    result = asyncio.run(service._run_ingestion(_record("v1", DocumentStatus.queued), b"data"))

    assert result.status == DocumentStatus.indexed

    # R5.1: a Document_Version manifest was created for the ingested version.
    manifest_payload = store.get_json(document_version_key("doc-1", "v1"))
    assert manifest_payload is not None
    manifest = DocumentVersion.model_validate(manifest_payload)
    assert manifest.version == "v1"
    assert manifest.indexed is True
    assert manifest.vectors_present is True
    assert manifest.source_retained is True

    # R5.1: exactly one succeeded Ingestion_Event was recorded.
    events = _events(store)
    assert len(events) == 1
    assert events[0].status == "succeeded"
    assert events[0].version == "v1"
    assert events[0].error is None

    # R5.2/R5.4: the version index lists the version and marks it active.
    index = _index_of(store)
    assert index.active_version == "v1"
    assert [v.version for v in index.versions] == ["v1"]


def test_second_version_activates_and_retains_prior_versions() -> None:
    store = CasStore()
    store.put_json(
        document_record_key("doc-1"),
        _record("v1", DocumentStatus.queued).model_dump(mode="json"),
    )
    service = _service(store, VersionedFakeIndex())

    asyncio.run(service._run_ingestion(_record("v1", DocumentStatus.queued), b"data-1"))
    # Simulate a replacement upload: record now carries v2, active still v1.
    store.put_json(
        document_record_key("doc-1"),
        _record("v2", DocumentStatus.queued, active_version="v1").model_dump(mode="json"),
    )
    asyncio.run(
        service._run_ingestion(
            _record("v2", DocumentStatus.queued, active_version="v1"), b"data-2"
        )
    )

    index = _index_of(store)
    # R5.4: exactly one active version, the newest.
    assert index.active_version == "v2"
    # R5.5/R5.11: prior version entries are retained, newest appended last.
    assert [v.version for v in index.versions] == ["v1", "v2"]

    # Two succeeded events across the two ingestions.
    events = _events(store)
    assert len(events) == 2
    assert all(e.status == "succeeded" for e in events)


def test_reingesting_same_version_is_idempotent_for_manifest() -> None:
    store = CasStore()
    store.put_json(
        document_record_key("doc-1"),
        _record("v1", DocumentStatus.queued).model_dump(mode="json"),
    )
    service = _service(store, VersionedFakeIndex())

    asyncio.run(service._run_ingestion(_record("v1", DocumentStatus.queued), b"data"))
    # Re-ingest identical content (same content-hash version) -> manifest key
    # already exists; the create-only write must be a no-op, not an error.
    asyncio.run(service._run_ingestion(_record("v1", DocumentStatus.queued), b"data"))

    index = _index_of(store)
    # The version appears exactly once even after re-ingestion.
    assert [v.version for v in index.versions] == ["v1"]
    assert index.active_version == "v1"


# ---------------------------------------------------------------------------
# R5.3 — failed ingestion.
# ---------------------------------------------------------------------------


def test_failed_ingestion_creates_no_version_and_records_failed_event() -> None:
    store = CasStore()
    store.put_json(
        document_record_key("doc-1"),
        _record("v2", DocumentStatus.queued, active_version="v1").model_dump(mode="json"),
    )
    # Fail on the embedding manifest write (runs after vectors are upserted).
    store.fail_put_json_when = lambda k, p: isinstance(p, dict) and "chunk_count" in p
    service = _service(store, VersionedFakeIndex())

    with pytest.raises(RuntimeError):
        asyncio.run(
            service._run_ingestion(
                _record("v2", DocumentStatus.queued, active_version="v1"), b"new-data"
            )
        )

    # R5.3: no Document_Version manifest for the failed version.
    assert store.get_json(document_version_key("doc-1", "v2")) is None
    # R5.3: no version index was created (active pointer untouched).
    assert store.get_json(document_version_index_key("doc-1")) is None

    # R5.3: a failed Ingestion_Event was recorded with the error.
    events = _events(store)
    assert len(events) == 1
    assert events[0].status == "failed"
    assert events[0].version == "v2"
    assert events[0].error


def test_failed_ingestion_leaves_prior_active_version_untouched() -> None:
    store = CasStore()
    store.put_json(
        document_record_key("doc-1"),
        _record("v1", DocumentStatus.queued).model_dump(mode="json"),
    )
    service = _service(store, VersionedFakeIndex())

    # First ingestion succeeds and publishes v1.
    asyncio.run(service._run_ingestion(_record("v1", DocumentStatus.queued), b"data-1"))
    assert _index_of(store).active_version == "v1"

    # A replacement v2 is now in flight but its ingestion fails.
    store.put_json(
        document_record_key("doc-1"),
        _record("v2", DocumentStatus.queued, active_version="v1").model_dump(mode="json"),
    )
    store.fail_put_json_when = lambda k, p: isinstance(p, dict) and "chunk_count" in p
    with pytest.raises(RuntimeError):
        asyncio.run(
            service._run_ingestion(
                _record("v2", DocumentStatus.queued, active_version="v1"), b"data-2"
            )
        )

    index = _index_of(store)
    # R5.3: active version stays v1; v2 was never added to the index.
    assert index.active_version == "v1"
    assert [v.version for v in index.versions] == ["v1"]

    # History carries the succeeded v1 event and the failed v2 event.
    events = sorted(_events(store), key=lambda e: e.version)
    assert [(e.version, e.status) for e in events] == [
        ("v1", "succeeded"),
        ("v2", "failed"),
    ]
