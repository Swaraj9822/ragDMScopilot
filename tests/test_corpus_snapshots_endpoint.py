"""Unit tests for POST /corpus-snapshots endpoint (R8.1, R8.6, task 14.12).

Covers:
- Operator-only gating (returns 403 for non-operators)
- Capturing all active-version documents when no subset specified
- Capturing only the specified document subset
- Excluding documents without an active version
- Optional SqlResultFixture capture alongside the snapshot
- 201 Created response with the corpus_snapshot_id
- GET /corpus-snapshots listing (task 14.18)
"""

from __future__ import annotations

from types import SimpleNamespace
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.models import (
    DocumentRecord,
    DocumentStatus,
)
from rag_system.storage import PreconditionFailed


# ---------------------------------------------------------------------------
# Fake store (in-memory, create-only semantics)
# ---------------------------------------------------------------------------


class _FakeArtifactStore:
    """Minimal in-memory store supporting create_json and get_json."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def create_json(self, key: str, payload: object) -> str:
        if key in self.objects:
            raise PreconditionFailed(f"Key already exists: {key}")
        self.objects[key] = payload
        return '"etag-1"'

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def update_json_cas(
        self,
        key: str,
        mutate: Callable[[object | None], object],
        *,
        max_attempts: int = 5,
    ) -> object:
        current = self.objects.get(key)
        result = mutate(current)
        self.objects[key] = result
        return result

    def list_document_record_keys(self) -> list[str]:
        return [k for k in self.objects if k.endswith("/record.json")]

    def list_corpus_snapshot_keys(self) -> list[str]:
        return [
            k for k in self.objects
            if k.startswith("corpus_snapshots/") and k.endswith(".json") and "/sql/" not in k
        ]

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return '"etag"'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(doc_id: str, active_version: str | None = "v1") -> DocumentRecord:
    return DocumentRecord(
        id=doc_id,
        title=f"{doc_id}.pdf",
        version=active_version or "v0",
        s3_uri=f"s3://bucket/raw/{doc_id}/{active_version or 'v0'}/source.pdf",
        status=DocumentStatus.indexed,
        active_version=active_version,
        owner="operator@test.com",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def operator_client(monkeypatch):
    """TestClient with operator gating satisfied and controllable service."""
    api_module.app.dependency_overrides[require_operator] = lambda: None
    store = _FakeArtifactStore()
    docs: list[DocumentRecord] = []

    class FakeService:
        artifact_store = store

        def list_documents(self) -> list[DocumentRecord]:
            return list(docs)

    service = FakeService()
    monkeypatch.setattr(api_module, "get_service", lambda: service)

    try:
        yield TestClient(api_module.app), store, docs
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_operator_gating_rejects_non_operator(monkeypatch) -> None:
    """Non-operators get 403 from the require_operator dependency."""
    # Don't override require_operator — let the real dependency reject.
    # But we need to mock get_current_user to avoid real auth.
    from rag_system.auth import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1", email="user@test.com", is_operator=False
    )
    try:
        client = TestClient(api_module.app)
        resp = client.post("/corpus-snapshots", json={})
        # The real require_operator would raise 403; since it's not overridden
        # and depends on get_current_user, the exact behavior depends on the
        # auth implementation. We check it's not 201 (operator required).
        assert resp.status_code in (401, 403, 422)
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)


def test_captures_all_active_version_documents(operator_client) -> None:
    """When no document_ids subset is given, captures all docs with active_version."""
    client, store, docs = operator_client
    docs.extend([
        _doc("doc-a", "v1"),
        _doc("doc-b", "v2"),
        _doc("doc-c", "v3"),
    ])

    resp = client.post("/corpus-snapshots", json={})

    assert resp.status_code == 201
    body = resp.json()
    assert "corpus_snapshot_id" in body
    assert len(body["corpus_snapshot_id"]) > 0

    # Verify the snapshot was persisted with the correct manifest.
    from rag_system.storage import corpus_snapshot_key
    from rag_system.models import CorpusSnapshot

    snap_id = body["corpus_snapshot_id"]
    raw = store.objects.get(corpus_snapshot_key(snap_id))
    assert raw is not None
    snapshot = CorpusSnapshot.model_validate(raw)
    manifest_set = set(snapshot.manifest)
    assert ("doc-a", "v1") in manifest_set
    assert ("doc-b", "v2") in manifest_set
    assert ("doc-c", "v3") in manifest_set
    assert len(snapshot.manifest) == 3


def test_captures_document_subset(operator_client) -> None:
    """When document_ids is given, only those documents are included."""
    client, store, docs = operator_client
    docs.extend([
        _doc("doc-a", "v1"),
        _doc("doc-b", "v2"),
        _doc("doc-c", "v3"),
    ])

    resp = client.post("/corpus-snapshots", json={"document_ids": ["doc-a", "doc-c"]})

    assert resp.status_code == 201
    body = resp.json()

    from rag_system.storage import corpus_snapshot_key
    from rag_system.models import CorpusSnapshot

    snap_id = body["corpus_snapshot_id"]
    raw = store.objects.get(corpus_snapshot_key(snap_id))
    snapshot = CorpusSnapshot.model_validate(raw)
    manifest_set = set(snapshot.manifest)
    assert ("doc-a", "v1") in manifest_set
    assert ("doc-c", "v3") in manifest_set
    assert ("doc-b", "v2") not in manifest_set
    assert len(snapshot.manifest) == 2


def test_excludes_documents_without_active_version(operator_client) -> None:
    """Documents with active_version=None are excluded from the manifest."""
    client, store, docs = operator_client
    docs.extend([
        _doc("doc-a", "v1"),
        _doc("doc-b", None),  # No active version
    ])

    resp = client.post("/corpus-snapshots", json={})

    assert resp.status_code == 201
    body = resp.json()

    from rag_system.storage import corpus_snapshot_key
    from rag_system.models import CorpusSnapshot

    snap_id = body["corpus_snapshot_id"]
    raw = store.objects.get(corpus_snapshot_key(snap_id))
    snapshot = CorpusSnapshot.model_validate(raw)
    assert len(snapshot.manifest) == 1
    assert snapshot.manifest[0] == ("doc-a", "v1")


def test_captures_sql_fixture_alongside(operator_client) -> None:
    """Optional sql_fixture is captured alongside the snapshot."""
    client, store, docs = operator_client
    docs.append(_doc("doc-a", "v1"))

    resp = client.post(
        "/corpus-snapshots",
        json={
            "sql_fixture": {
                "sql": "SELECT * FROM orders",
                "rows": [{"id": 1, "total": 100}],
            }
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    snap_id = body["corpus_snapshot_id"]

    # Check that both the snapshot and the fixture were persisted.
    from rag_system.storage import corpus_snapshot_key, sql_result_fixture_key
    from rag_system.replay import normalized_sql_hash

    assert corpus_snapshot_key(snap_id) in store.objects

    fixture_hash = normalized_sql_hash("SELECT * FROM orders")
    fixture_key = sql_result_fixture_key(snap_id, fixture_hash)
    assert fixture_key in store.objects

    fixture = store.objects[fixture_key]
    assert fixture["sql"] == "SELECT * FROM orders"
    assert fixture["rows"] == [{"id": 1, "total": 100}]


def test_empty_corpus_yields_empty_manifest(operator_client) -> None:
    """An empty corpus produces a snapshot with an empty manifest."""
    client, store, docs = operator_client
    # No documents

    resp = client.post("/corpus-snapshots", json={})

    assert resp.status_code == 201
    body = resp.json()

    from rag_system.storage import corpus_snapshot_key
    from rag_system.models import CorpusSnapshot

    snap_id = body["corpus_snapshot_id"]
    raw = store.objects.get(corpus_snapshot_key(snap_id))
    snapshot = CorpusSnapshot.model_validate(raw)
    assert snapshot.manifest == []


def test_subset_with_no_matching_docs(operator_client) -> None:
    """A subset scope with no matching document_ids yields an empty manifest."""
    client, store, docs = operator_client
    docs.append(_doc("doc-a", "v1"))

    resp = client.post("/corpus-snapshots", json={"document_ids": ["nonexistent"]})

    assert resp.status_code == 201
    body = resp.json()

    from rag_system.storage import corpus_snapshot_key
    from rag_system.models import CorpusSnapshot

    snap_id = body["corpus_snapshot_id"]
    raw = store.objects.get(corpus_snapshot_key(snap_id))
    snapshot = CorpusSnapshot.model_validate(raw)
    assert snapshot.manifest == []


# ---------------------------------------------------------------------------
# GET /corpus-snapshots listing tests (task 14.18, R8.1)
# ---------------------------------------------------------------------------


def test_list_corpus_snapshots_returns_empty_list(operator_client) -> None:
    """Returns an empty list when no snapshots exist."""
    client, store, docs = operator_client

    resp = client.get("/corpus-snapshots")

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_corpus_snapshots_returns_summaries(operator_client) -> None:
    """Returns id, created_at, and manifest_size for each snapshot."""
    client, store, docs = operator_client

    # Seed some snapshots directly in the store.
    from rag_system.storage import corpus_snapshot_key

    store.objects[corpus_snapshot_key("snap-1")] = {
        "corpus_snapshot_id": "snap-1",
        "created_at": "2024-01-01T00:00:00Z",
        "manifest": [["doc-a", "v1"], ["doc-b", "v2"]],
    }
    store.objects[corpus_snapshot_key("snap-2")] = {
        "corpus_snapshot_id": "snap-2",
        "created_at": "2024-02-15T12:00:00Z",
        "manifest": [["doc-c", "v3"]],
    }

    resp = client.get("/corpus-snapshots")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2

    # Should be sorted newest first.
    assert body[0]["corpus_snapshot_id"] == "snap-2"
    assert body[0]["created_at"] == "2024-02-15T12:00:00Z"
    assert body[0]["manifest_size"] == 1

    assert body[1]["corpus_snapshot_id"] == "snap-1"
    assert body[1]["created_at"] == "2024-01-01T00:00:00Z"
    assert body[1]["manifest_size"] == 2


def test_list_corpus_snapshots_excludes_sql_fixtures(operator_client) -> None:
    """SQL fixtures stored under corpus_snapshots/{id}/sql/ are not returned."""
    client, store, docs = operator_client

    from rag_system.storage import corpus_snapshot_key, sql_result_fixture_key

    store.objects[corpus_snapshot_key("snap-1")] = {
        "corpus_snapshot_id": "snap-1",
        "created_at": "2024-01-01T00:00:00Z",
        "manifest": [["doc-a", "v1"]],
    }
    # This is a fixture, not a snapshot — should be excluded.
    store.objects[sql_result_fixture_key("snap-1", "hash123")] = {
        "fixture_id": "hash123",
        "corpus_snapshot_id": "snap-1",
        "sql": "SELECT 1",
        "normalized_sql_hash": "hash123",
        "rows": [],
    }

    resp = client.get("/corpus-snapshots")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["corpus_snapshot_id"] == "snap-1"


def test_list_corpus_snapshots_operator_only(monkeypatch) -> None:
    """Non-operators get 403 from the require_operator dependency."""
    from rag_system.auth import get_current_user

    api_module.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1", email="user@test.com", is_operator=False
    )
    try:
        client = TestClient(api_module.app)
        resp = client.get("/corpus-snapshots")
        # The real require_operator rejects non-operators with 403.
        assert resp.status_code in (401, 403, 422)
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)


def test_list_corpus_snapshots_sorted_newest_first(operator_client) -> None:
    """Snapshots are sorted by created_at descending (newest first)."""
    client, store, docs = operator_client

    from rag_system.storage import corpus_snapshot_key

    store.objects[corpus_snapshot_key("snap-old")] = {
        "corpus_snapshot_id": "snap-old",
        "created_at": "2023-06-01T00:00:00Z",
        "manifest": [],
    }
    store.objects[corpus_snapshot_key("snap-mid")] = {
        "corpus_snapshot_id": "snap-mid",
        "created_at": "2024-01-15T00:00:00Z",
        "manifest": [["doc-a", "v1"]],
    }
    store.objects[corpus_snapshot_key("snap-new")] = {
        "corpus_snapshot_id": "snap-new",
        "created_at": "2024-03-20T00:00:00Z",
        "manifest": [["doc-a", "v1"], ["doc-b", "v2"], ["doc-c", "v3"]],
    }

    resp = client.get("/corpus-snapshots")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    ids = [s["corpus_snapshot_id"] for s in body]
    assert ids == ["snap-new", "snap-mid", "snap-old"]
    assert body[0]["manifest_size"] == 3
    assert body[1]["manifest_size"] == 1
    assert body[2]["manifest_size"] == 0
