"""Regression tests for atomic index publication (finding: non-atomic publish).

Before the fix, replacing a document deleted its vectors *before* the new
version was ingested, and batched upserts became searchable incrementally. A
failure then left partial vectors searchable with the previous good version
gone.

The fix publishes by version: ingestion upserts the new version's vectors, then
atomically switches the record's ``active_version`` pointer, then garbage-
collects superseded vectors. Reads are gated to the active version, so in-flight
partials and leftover previous-version vectors are never returned even though
they physically coexist in the index. These tests exercise that behaviour with a
version-aware in-memory index and a CAS-capable store double.
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
    RetrievalHit,
)
from rag_system.service import RagService, content_hash
from rag_system.storage import PreconditionFailed, document_record_key


# ---------------------------------------------------------------------------
# Store double: CAS semantics + the puts the ingestion pipeline needs.
# ---------------------------------------------------------------------------


class CasStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        #: When set, put_json raises for the manifest write to simulate a
        #: post-upsert ingestion failure. Signature: (key, payload) -> bool.
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


# ---------------------------------------------------------------------------
# Version-aware index double: vectors carry (document_id, version).
# ---------------------------------------------------------------------------


class VersionedFakeIndex:
    def __init__(self) -> None:
        self.vectors: list[EmbeddedChunk] = []
        self.calls: list[tuple[str, str, str | None]] = []

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        self.vectors.extend(embedded_chunks)

    def delete_document(self, document_id: str) -> None:
        self.calls.append(("all", document_id, None))
        self.vectors = [v for v in self.vectors if v.chunk.document_id != document_id]

    def delete_document_version(self, document_id: str, version: str) -> None:
        self.calls.append(("version", document_id, version))
        self.vectors = [
            v
            for v in self.vectors
            if not (v.chunk.document_id == document_id and v.chunk.version == version)
        ]

    def delete_document_except_version(self, document_id: str, keep_version: str) -> None:
        self.calls.append(("except", document_id, keep_version))
        self.vectors = [
            v
            for v in self.vectors
            if not (v.chunk.document_id == document_id and v.chunk.version != keep_version)
        ]

    def search(self, query_vector, top_k, document_ids=None, sparse_vector=None):
        hits = [RetrievalHit(chunk=v.chunk, score=0.9, source="fake") for v in self.vectors]
        if document_ids:
            hits = [h for h in hits if h.chunk.document_id in document_ids]
        return hits[:top_k]

    def versions_for(self, document_id: str) -> set[str]:
        return {v.chunk.version for v in self.vectors if v.chunk.document_id == document_id}


# ---------------------------------------------------------------------------
# Ingestion-pipeline fakes.
# ---------------------------------------------------------------------------


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


class FailingEmbedder:
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        raise RuntimeError("embedding blew up")


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[object] = []

    def enqueue(self, job) -> str:
        self.jobs.append(job)
        return "message-1"


def _service(
    store: CasStore,
    index: VersionedFakeIndex,
    *,
    embedder=None,
    queue: FakeQueue | None = None,
) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        bedrock_embedding_model_id="embed-model",
        s3_bucket="bucket",
        pinecone_index_name="index",
    )
    service._store = store
    service._queue = queue
    service._documents = {}
    service._parser = FakeParser()
    service._chunker = FakeChunker()
    service._embedder = embedder if embedder is not None else FakeEmbedder()
    service._sparse_encoder = None
    service._index = index
    service._reranker = None
    service._generator = None
    return service


def _record(version: str, status: DocumentStatus, active_version: str | None = None) -> DocumentRecord:
    return DocumentRecord(
        id="doc-1",
        title="source.pdf",
        version=version,
        s3_uri=f"s3://bucket/raw/doc-1/{version}/source.pdf",
        status=status,
        active_version=active_version,
    )


# ---------------------------------------------------------------------------
# Publication switch + cleanup.
# ---------------------------------------------------------------------------


def test_first_ingestion_sets_active_version_and_cleans_up() -> None:
    store = CasStore()
    key = document_record_key("doc-1")
    store.put_json(key, _record("v1", DocumentStatus.queued).model_dump(mode="json"))
    index = VersionedFakeIndex()
    service = _service(store, index)

    result = asyncio.run(service._run_ingestion(_record("v1", DocumentStatus.queued), b"data"))

    assert result.status == DocumentStatus.indexed
    assert result.active_version == "v1"
    assert store.get_json(key)["active_version"] == "v1"
    assert index.versions_for("doc-1") == {"v1"}
    # Cleanup of superseded versions ran after the publish switch.
    assert ("except", "doc-1", "v1") in index.calls


def test_replacement_publishes_new_version_and_removes_old_vectors() -> None:
    store = CasStore()
    key = document_record_key("doc-1")
    # A previous version v1 is published and its vectors are in the index.
    store.put_json(
        key, _record("v2", DocumentStatus.queued, active_version="v1").model_dump(mode="json")
    )
    index = VersionedFakeIndex()
    old_chunk = Chunk(id="doc-1:v1:0", document_id="doc-1", version="v1", text="old")
    index.upsert([EmbeddedChunk(chunk=old_chunk, dense_vector=[0.1, 0.2])])
    service = _service(store, index)

    result = asyncio.run(
        service._run_ingestion(
            _record("v2", DocumentStatus.queued, active_version="v1"), b"new-data"
        )
    )

    assert result.status == DocumentStatus.indexed
    assert result.active_version == "v2"
    # Old version's vectors were cleaned up after publishing v2.
    assert index.versions_for("doc-1") == {"v2"}


def test_failed_ingestion_removes_partials_and_preserves_previous_version() -> None:
    store = CasStore()
    key = document_record_key("doc-1")
    store.put_json(
        key, _record("v2", DocumentStatus.queued, active_version="v1").model_dump(mode="json")
    )
    index = VersionedFakeIndex()
    # Previous published version v1 is present and must survive the failure.
    old_chunk = Chunk(id="doc-1:v1:0", document_id="doc-1", version="v1", text="old")
    index.upsert([EmbeddedChunk(chunk=old_chunk, dense_vector=[0.1, 0.2])])
    # Fail on the manifest write, which runs *after* the v2 vectors are upserted.
    store.fail_put_json_when = lambda k, p: isinstance(p, dict) and "chunk_count" in p
    service = _service(store, index)

    with pytest.raises(RuntimeError):
        asyncio.run(
            service._run_ingestion(
                _record("v2", DocumentStatus.queued, active_version="v1"), b"new-data"
            )
        )

    # The failed version's partial vectors were removed; v1 still searchable.
    assert index.versions_for("doc-1") == {"v1"}
    assert ("version", "doc-1", "v2") in index.calls
    # The record is marked failed but still points at the good published version.
    stored = store.get_json(key)
    assert stored["status"] == DocumentStatus.failed
    assert stored["active_version"] == "v1"


# ---------------------------------------------------------------------------
# Replacement no longer pre-deletes vectors.
# ---------------------------------------------------------------------------


def test_update_document_does_not_predelete_and_carries_active_version() -> None:
    store = CasStore()
    key = document_record_key("doc-1")
    store.put_json(
        key,
        _record("v1", DocumentStatus.indexed, active_version="v1").model_dump(mode="json"),
    )
    index = VersionedFakeIndex()
    old_chunk = Chunk(id="doc-1:v1:0", document_id="doc-1", version="v1", text="old")
    index.upsert([EmbeddedChunk(chunk=old_chunk, dense_vector=[0.1, 0.2])])
    queue = FakeQueue()
    service = _service(store, index, queue=queue)

    result = asyncio.run(service.update_document("doc-1", "updated.pdf", b"new-bytes"))

    assert result is not None
    assert result.status == DocumentStatus.queued
    assert result.version == content_hash(b"new-bytes")
    # The published version is carried forward so v1 stays searchable meanwhile.
    assert result.active_version == "v1"
    # No vectors were deleted up front, and v1's vectors remain in the index.
    assert index.calls == []
    assert index.versions_for("doc-1") == {"v1"}
    assert len(queue.jobs) == 1


# ---------------------------------------------------------------------------
# The read gate: only the active version is searchable.
# ---------------------------------------------------------------------------


def _hit(version: str) -> RetrievalHit:
    chunk = Chunk(id=f"doc-1:{version}:0", document_id="doc-1", version=version, text="t")
    return RetrievalHit(chunk=chunk, score=0.9, source="fake")


@pytest.mark.parametrize(
    "record,expected_versions",
    [
        # Published v1: only v1 hits survive; a not-yet-published v2 is hidden.
        (_record("v2", DocumentStatus.indexed, active_version="v1"), {"v1"}),
        # Mid-replacement (still parsing v2, active pointer at v1): v2 hidden.
        (_record("v2", DocumentStatus.parsing, active_version="v1"), {"v1"}),
        # Fresh in-flight first ingestion (nothing published yet): all hidden.
        (_record("v1", DocumentStatus.queued, active_version=None), set()),
        # Legacy indexed record written before active_version existed: falls
        # back to `version`, so v1 is served.
        (_record("v1", DocumentStatus.indexed, active_version=None), {"v1"}),
        # Deleted record: nothing is searchable.
        (_record("v1", DocumentStatus.deleted, active_version="v1"), set()),
    ],
)
def test_retrieval_gate_filters_to_active_version(record, expected_versions) -> None:
    store = CasStore()
    index = VersionedFakeIndex()
    service = _service(store, index)
    service._documents = {"doc-1": record}

    kept = service._filter_to_active_versions([_hit("v1"), _hit("v2")], {})

    assert {hit.chunk.version for hit in kept} == expected_versions


def test_retrieval_gate_hides_hits_for_unknown_document() -> None:
    # No record anywhere -> fail closed (orphaned vectors are not served).
    store = CasStore()
    index = VersionedFakeIndex()
    service = _service(store, index)

    kept = service._filter_to_active_versions([_hit("v1")], {})

    assert kept == []
