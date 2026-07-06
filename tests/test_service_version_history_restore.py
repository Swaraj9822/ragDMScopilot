"""Tests for document version history and restore (R5.6-R5.11).

These exercise ``RagService.get_document_history`` and
``RagService.restore_version`` directly against a seeded, CAS-capable store
double, plus the ``api.py`` wiring for ``GET /documents/{id}/versions`` and
``POST /documents/{id}/versions/{version}/restore``.

Covered behavior:
* history returns versions + ingestion events newest-first (R5.7);
* restore flips the active pointer when the target's vectors still exist
  (R5.8) and retains every prior version (R5.11);
* restore re-indexes from retained source content when the target's vectors
  were cleaned up (R5.9);
* restoring an unknown version leaves the active version unchanged and raises
  ``DocumentVersionNotFoundError`` (R5.10);
* retrieval uses the active version after a restore (R5.6);
* the API translates the not-found error into ``404 version_not_found``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth.dependencies import require_operator
from rag_system.models import (
    Chunk,
    DocumentHistory,
    DocumentRecord,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionIndex,
    EmbeddedChunk,
    IngestionEvent,
)
from rag_system.service import DocumentVersionNotFoundError, RagService
from rag_system.storage import (
    PreconditionFailed,
    document_record_key,
    document_version_index_key,
    ingestion_event_key,
)

DOC = "doc-1"


# ---------------------------------------------------------------------------
# CAS-capable store double with history/restore support.
# ---------------------------------------------------------------------------


class CasStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        #: Retained chunk content keyed by (document_id, version) for re-index.
        self.chunks: dict[tuple[str, str], list[Chunk]] = {}

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    def get_json(self, key: str) -> object | None:
        entry = self.objects.get(key)
        return None if entry is None else entry[0]

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = (payload, self._next_etag())
        return f"s3://bucket/{key}"

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

    def list_ingestion_event_keys(self, document_id: str) -> list[str]:
        prefix = f"documents/{document_id}/ingestions/"
        return [k for k in self.objects if k.startswith(prefix)]

    def get_chunks(self, document_id: str, version: str) -> list[Chunk]:
        return list(self.chunks.get((document_id, version), []))


class VersionedFakeIndex:
    def __init__(self) -> None:
        self.vectors: list[EmbeddedChunk] = []

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        self.vectors.extend(embedded_chunks)


class FakeEmbedder:
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [EmbeddedChunk(chunk=chunk, dense_vector=[0.1, 0.2]) for chunk in chunks]


def _service(store: CasStore, index: VersionedFakeIndex | None = None) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        gcs_bucket="bucket",
        pinecone_index_name="index",
    )
    service._store = store
    service._queue = None
    service._documents = {}
    service._parser = None
    service._chunker = None
    service._embedder = FakeEmbedder()
    service._sparse_encoder = None
    service._index = index or VersionedFakeIndex()
    service._generator = None
    return service


def _version(version: str, created_at: str, *, vectors_present: bool = True) -> DocumentVersion:
    return DocumentVersion(
        document_id=DOC,
        version=version,
        created_at=created_at,
        indexed=True,
        vectors_present=vectors_present,
        source_retained=True,
    )


def _event(version: str, timestamp: str, status: str = "succeeded") -> IngestionEvent:
    return IngestionEvent(
        ingestion_id=f"ing-{version}-{timestamp}",
        document_id=DOC,
        version=version,
        status=status,
        timestamp=timestamp,
    )


def _seed(
    store: CasStore,
    *,
    versions: list[DocumentVersion],
    active_version: str | None,
    record_version: str,
    events: list[IngestionEvent] | None = None,
    status: DocumentStatus = DocumentStatus.indexed,
) -> None:
    record = DocumentRecord(
        id=DOC,
        title="source.pdf",
        version=record_version,
        s3_uri=f"s3://bucket/raw/{DOC}/{record_version}/source.pdf",
        status=status,
        active_version=active_version,
    )
    store.put_json(document_record_key(DOC), record.model_dump(mode="json"))
    index = DocumentVersionIndex(
        document_id=DOC, active_version=active_version, versions=versions
    )
    store.put_json(document_version_index_key(DOC), index.model_dump(mode="json"))
    for event in events or []:
        store.put_json(
            ingestion_event_key(DOC, event.ingestion_id),
            event.model_dump(mode="json"),
        )


def _index_of(store: CasStore) -> DocumentVersionIndex:
    payload = store.get_json(document_version_index_key(DOC))
    assert payload is not None
    return DocumentVersionIndex.model_validate(payload)


# ---------------------------------------------------------------------------
# R5.7 — history ordering.
# ---------------------------------------------------------------------------


def test_history_orders_versions_and_events_newest_first() -> None:
    store = CasStore()
    _seed(
        store,
        versions=[
            _version("v1", "2024-01-01T00:00:00+00:00"),
            _version("v2", "2024-01-02T00:00:00+00:00"),
            _version("v3", "2024-01-03T00:00:00+00:00"),
        ],
        active_version="v3",
        record_version="v3",
        events=[
            _event("v1", "2024-01-01T00:00:00+00:00"),
            _event("v2", "2024-01-02T00:00:00+00:00"),
            _event("v3", "2024-01-03T00:00:00+00:00", status="failed"),
        ],
    )
    service = _service(store)

    history = service.get_document_history(DOC)

    assert history is not None
    # R5.7: newest first for both versions and events.
    assert [v.version for v in history.versions] == ["v3", "v2", "v1"]
    assert [e.version for e in history.events] == ["v3", "v2", "v1"]
    assert history.active_version == "v3"


def test_history_none_for_missing_document() -> None:
    service = _service(CasStore())
    assert service.get_document_history("missing") is None


def test_history_none_for_deleted_document() -> None:
    store = CasStore()
    _seed(
        store,
        versions=[_version("v1", "2024-01-01T00:00:00+00:00")],
        active_version="v1",
        record_version="v1",
        status=DocumentStatus.deleted,
    )
    assert _service(store).get_document_history(DOC) is None


# ---------------------------------------------------------------------------
# R5.8, R5.11, R5.6 — restore with vectors present.
# ---------------------------------------------------------------------------


def test_restore_flips_active_when_vectors_present() -> None:
    store = CasStore()
    _seed(
        store,
        versions=[
            _version("v1", "2024-01-01T00:00:00+00:00", vectors_present=True),
            _version("v2", "2024-01-02T00:00:00+00:00", vectors_present=True),
        ],
        active_version="v2",
        record_version="v2",
    )
    index = VersionedFakeIndex()
    service = _service(store, index)

    restored = service.restore_version(DOC, "v1")

    assert restored is not None
    # R5.8: the active pointer flips to the restored version.
    assert restored.active_version == "v1"
    assert restored.status == DocumentStatus.indexed
    updated = _index_of(store)
    assert updated.active_version == "v1"
    # R5.11: all prior versions are retained in the index.
    assert sorted(v.version for v in updated.versions) == ["v1", "v2"]
    # Only the restored version holds live vectors after the flip.
    assert {v.version: v.vectors_present for v in updated.versions} == {
        "v1": True,
        "v2": False,
    }
    # R5.6: retrieval's search-gate now resolves the restored active version.
    assert service._active_version_for(DOC) == "v1"
    # Vectors already existed, so no re-index upsert occurred.
    assert index.vectors == []


# ---------------------------------------------------------------------------
# R5.9 — restore re-indexes from retained source when vectors cleaned up.
# ---------------------------------------------------------------------------


def test_restore_reindexes_when_vectors_cleaned_up() -> None:
    store = CasStore()
    _seed(
        store,
        versions=[
            _version("v1", "2024-01-01T00:00:00+00:00", vectors_present=False),
            _version("v2", "2024-01-02T00:00:00+00:00", vectors_present=True),
        ],
        active_version="v2",
        record_version="v2",
    )
    # Retained source content for the cleaned-up version.
    store.chunks[(DOC, "v1")] = [
        Chunk(id=f"{DOC}:v1:0", document_id=DOC, version="v1", text="retained body")
    ]
    index = VersionedFakeIndex()
    service = _service(store, index)

    restored = service.restore_version(DOC, "v1")

    assert restored is not None
    assert restored.active_version == "v1"
    # R5.9: the retained source was re-embedded and upserted before activation.
    assert [ec.chunk.version for ec in index.vectors] == ["v1"]
    assert index.vectors[0].chunk.text == "retained body"
    assert _index_of(store).active_version == "v1"


def test_restore_reindex_fails_when_no_retained_source() -> None:
    store = CasStore()
    _seed(
        store,
        versions=[
            _version("v1", "2024-01-01T00:00:00+00:00", vectors_present=False),
            _version("v2", "2024-01-02T00:00:00+00:00", vectors_present=True),
        ],
        active_version="v2",
        record_version="v2",
    )
    # No retained chunks seeded for v1 -> re-index cannot proceed.
    service = _service(store)

    with pytest.raises(RuntimeError):
        service.restore_version(DOC, "v1")

    # Active version must remain unchanged when the restore cannot complete.
    assert _index_of(store).active_version == "v2"


# ---------------------------------------------------------------------------
# R5.10 — unknown version.
# ---------------------------------------------------------------------------


def test_restore_unknown_version_raises_and_leaves_active_unchanged() -> None:
    store = CasStore()
    _seed(
        store,
        versions=[
            _version("v1", "2024-01-01T00:00:00+00:00"),
            _version("v2", "2024-01-02T00:00:00+00:00"),
        ],
        active_version="v2",
        record_version="v2",
    )
    service = _service(store)

    with pytest.raises(DocumentVersionNotFoundError):
        service.restore_version(DOC, "v99")

    # R5.10: the active version is left unchanged, all versions retained.
    updated = _index_of(store)
    assert updated.active_version == "v2"
    assert sorted(v.version for v in updated.versions) == ["v1", "v2"]
    assert service.get_document(DOC).active_version == "v2"


def test_restore_returns_none_for_missing_document() -> None:
    service = _service(CasStore())
    assert service.restore_version("missing", "v1") is None


# ---------------------------------------------------------------------------
# API wiring — GET versions & POST restore.
# ---------------------------------------------------------------------------


@pytest.fixture
def _operator_client(monkeypatch):
    """A TestClient with operator gating satisfied and a stub service slot."""
    api_module.app.dependency_overrides[require_operator] = lambda: None
    holder: dict[str, object] = {}
    monkeypatch.setattr(api_module, "get_service", lambda: holder["service"])
    try:
        yield TestClient(api_module.app), holder
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


def test_get_versions_endpoint_returns_history(_operator_client) -> None:
    client, holder = _operator_client
    history = DocumentHistory(
        document_id=DOC,
        active_version="v2",
        versions=[
            _version("v2", "2024-01-02T00:00:00+00:00"),
            _version("v1", "2024-01-01T00:00:00+00:00"),
        ],
        events=[_event("v2", "2024-01-02T00:00:00+00:00")],
    )
    holder["service"] = SimpleNamespace(
        get_document_history=lambda document_id: history
    )

    resp = client.get(f"/documents/{DOC}/versions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["active_version"] == "v2"
    assert [v["version"] for v in body["versions"]] == ["v2", "v1"]


def test_get_versions_endpoint_404_when_missing(_operator_client) -> None:
    client, holder = _operator_client
    holder["service"] = SimpleNamespace(get_document_history=lambda document_id: None)

    resp = client.get(f"/documents/{DOC}/versions")

    assert resp.status_code == 404


def test_restore_endpoint_returns_record(_operator_client) -> None:
    client, holder = _operator_client
    record = DocumentRecord(
        id=DOC,
        title="source.pdf",
        version="v2",
        s3_uri=f"s3://bucket/raw/{DOC}/v2/source.pdf",
        status=DocumentStatus.indexed,
        active_version="v1",
    )
    holder["service"] = SimpleNamespace(
        restore_version=lambda document_id, version: record
    )

    resp = client.post(f"/documents/{DOC}/versions/v1/restore")

    assert resp.status_code == 200
    assert resp.json()["active_version"] == "v1"


def test_restore_endpoint_404_version_not_found(_operator_client) -> None:
    client, holder = _operator_client

    def _raise(document_id: str, version: str):
        raise DocumentVersionNotFoundError(document_id, version)

    holder["service"] = SimpleNamespace(restore_version=_raise)

    resp = client.post(f"/documents/{DOC}/versions/v99/restore")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "version_not_found"


def test_restore_endpoint_404_when_document_missing(_operator_client) -> None:
    client, holder = _operator_client
    holder["service"] = SimpleNamespace(
        restore_version=lambda document_id, version: None
    )

    resp = client.post(f"/documents/{DOC}/versions/v1/restore")

    assert resp.status_code == 404
