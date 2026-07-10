"""Tests for role-based document ownership scoping (R4.11).

Covers the multi-user access model wired in ``api.py``:

* uploads stamp the authenticated caller as the document ``owner``;
* non-operators may only list/read/replace/delete documents they own (IDOR
  protection), while operators see the whole corpus;
* RAG retrieval is confined to the caller's owned documents — a non-operator
  with no accessible documents in scope retrieves nothing rather than falling
  through to an unscoped whole-index search.

The API tests override ``get_current_user`` (per-caller identity) and
``get_settings`` (a small auth-enabled stand-in) so the scoping logic is
exercised deterministically without a live database or the repo ``.env``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.api import _scoped_document_ids, _user_can_access_document
from rag_system.auth.dependencies import get_current_user
from rag_system.auth.models import UserPublic
from rag_system.config import get_settings
from rag_system.models import DocumentRecord, DocumentStatus
from rag_system.retrieval import PineconeHybridIndex

ALICE = "alice@tenant.example"
BOB = "bob@tenant.example"

# Auth-enabled settings stand-in with no allow-listed operators, so operator
# status is decided purely by the user's ``is_operator`` flag.
_SETTINGS = SimpleNamespace(auth_enabled=True, operator_emails_set=frozenset())


def _user(email: str, *, is_operator: bool = False) -> UserPublic:
    return UserPublic(
        id=email,
        email=email,
        is_active=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        is_operator=is_operator,
    )


def _doc(doc_id: str, owner: str | None) -> DocumentRecord:
    return DocumentRecord(
        id=doc_id,
        title=doc_id,
        version="v1",
        s3_uri=f"s3://bucket/{doc_id}",
        status=DocumentStatus.indexed,
        active_version="v1",
        owner=owner,
    )


class _FakeDocService:
    """Minimal document service double capturing owner and mutations."""

    def __init__(self, docs: list[DocumentRecord]) -> None:
        self._docs = {d.id: d for d in docs}
        self.deleted: list[str] = []
        self.queued_owner: str | None = None

    def list_documents(self) -> list[DocumentRecord]:
        return list(self._docs.values())

    def get_document(self, document_id: str) -> DocumentRecord | None:
        return self._docs.get(document_id)

    def delete_document(self, document_id: str) -> DocumentRecord | None:
        record = self._docs.get(document_id)
        if record is None:
            return None
        self.deleted.append(document_id)
        return record.model_copy(update={"status": DocumentStatus.deleted})

    async def queue_pdf(
        self, filename: str, content: bytes, owner: str | None = None
    ) -> DocumentRecord:
        self.queued_owner = owner
        return _doc("new-doc", owner)


@pytest.fixture
def client_as(monkeypatch):
    """Return a factory that yields a TestClient acting as a given user."""

    def _make(user: UserPublic, service: _FakeDocService) -> TestClient:
        api_module.app.dependency_overrides[get_current_user] = lambda: user
        api_module.app.dependency_overrides[get_settings] = lambda: _SETTINGS
        monkeypatch.setattr(api_module, "get_service", lambda: service)
        return TestClient(api_module.app)

    try:
        yield _make
    finally:
        api_module.app.dependency_overrides.pop(get_current_user, None)
        api_module.app.dependency_overrides.pop(get_settings, None)


# ---------------------------------------------------------------------------
# Pure helper unit tests.
# ---------------------------------------------------------------------------


def test_user_can_access_own_document() -> None:
    assert _user_can_access_document(_doc("d", ALICE), _user(ALICE), _SETTINGS)


def test_user_cannot_access_foreign_document() -> None:
    assert not _user_can_access_document(_doc("d", BOB), _user(ALICE), _SETTINGS)


def test_operator_can_access_any_document() -> None:
    assert _user_can_access_document(_doc("d", BOB), _user(ALICE, is_operator=True), _SETTINGS)


def test_legacy_ownerless_document_hidden_from_non_operator() -> None:
    assert not _user_can_access_document(_doc("d", None), _user(ALICE), _SETTINGS)


def test_scoped_ids_operator_is_unrestricted() -> None:
    # Operators pass through unchanged (None = whole corpus), with no listing.
    assert _scoped_document_ids(None, _user(ALICE, is_operator=True), _SETTINGS) is None


def test_scoped_ids_intersects_requested_with_owned(monkeypatch) -> None:
    service = _FakeDocService([_doc("a", ALICE), _doc("b", BOB), _doc("c", ALICE)])
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    # Requesting a mix returns only the caller's owned ids.
    assert _scoped_document_ids(["a", "b", "c"], _user(ALICE), _SETTINGS) == ["a", "c"]


def test_scoped_ids_defaults_to_all_owned(monkeypatch) -> None:
    service = _FakeDocService([_doc("a", ALICE), _doc("b", BOB)])
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    assert _scoped_document_ids(None, _user(ALICE), _SETTINGS) == ["a"]


def test_scoped_ids_empty_when_owns_nothing(monkeypatch) -> None:
    service = _FakeDocService([_doc("b", BOB)])
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    # Empty (not None): retrieval treats this as "match no documents".
    assert _scoped_document_ids(None, _user(ALICE), _SETTINGS) == []


def test_search_with_empty_scope_returns_no_hits() -> None:
    # An explicitly empty document scope must not fall through to a whole-index
    # search. Instantiate without __init__ so no Pinecone connection is opened;
    # the early return happens before ``_index`` is touched.
    index = object.__new__(PineconeHybridIndex)
    assert index.search(query_vector=[0.1, 0.2], top_k=5, document_ids=[]) == []


# ---------------------------------------------------------------------------
# API-level ownership enforcement.
# ---------------------------------------------------------------------------


def test_upload_stamps_uploader_as_owner(client_as) -> None:
    service = _FakeDocService([])
    client = client_as(_user(ALICE), service)
    resp = client.post(
        "/documents",
        files={"file": ("report.pdf", b"hello", "application/pdf")},
    )
    assert resp.status_code == 202
    assert service.queued_owner == ALICE


def test_list_documents_scoped_to_owner_for_non_operator(client_as) -> None:
    service = _FakeDocService([_doc("a", ALICE), _doc("b", BOB), _doc("c", None)])
    client = client_as(_user(ALICE), service)
    resp = client.get("/documents")
    assert resp.status_code == 200
    assert [d["id"] for d in resp.json()] == ["a"]


def test_list_documents_full_corpus_for_operator(client_as) -> None:
    service = _FakeDocService([_doc("a", ALICE), _doc("b", BOB)])
    client = client_as(_user("ops@x", is_operator=True), service)
    resp = client.get("/documents")
    assert resp.status_code == 200
    assert {d["id"] for d in resp.json()} == {"a", "b"}


def test_get_foreign_document_returns_404(client_as) -> None:
    service = _FakeDocService([_doc("b", BOB)])
    client = client_as(_user(ALICE), service)
    assert client.get("/documents/b").status_code == 404


def test_get_own_document_returns_200(client_as) -> None:
    service = _FakeDocService([_doc("a", ALICE)])
    client = client_as(_user(ALICE), service)
    resp = client.get("/documents/a")
    assert resp.status_code == 200
    assert resp.json()["id"] == "a"


def test_delete_foreign_document_returns_404_without_deleting(client_as) -> None:
    service = _FakeDocService([_doc("b", BOB)])
    client = client_as(_user(ALICE), service)
    assert client.delete("/documents/b").status_code == 404
    assert service.deleted == []


def test_delete_own_document_succeeds(client_as) -> None:
    service = _FakeDocService([_doc("a", ALICE)])
    client = client_as(_user(ALICE), service)
    assert client.delete("/documents/a").status_code == 200
    assert service.deleted == ["a"]
