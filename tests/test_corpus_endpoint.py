"""Integration tests for the GET /corpus endpoint (R4.1, R4.2, R4.3, R4.6, R4.14).

Feature: rag-trust-and-observability (task 8.6).

Covers:
- Endpoint registration and basic response shape (R4.1)
- Operator sees all documents; non-operator sees only owned (R4.2, R4.3)
- Sort/filter/search query params are forwarded correctly
- Invalid cursor returns 400 with detail ``invalid_cursor`` (R4.6)
- Search term >200 chars returns 400 with detail ``search_term_too_long`` (R4.14)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth.dependencies import get_current_user
from rag_system.auth.models import UserPublic
from rag_system.models import DocumentRecord, DocumentStatus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_OPERATOR_USER = UserPublic(
    id="op-user",
    email="operator@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=True,
)

_NORMAL_USER = UserPublic(
    id="normal-user",
    email="alice@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=False,
)


def _doc(
    doc_id: str,
    title: str,
    owner: str | None = None,
    status: DocumentStatus = DocumentStatus.indexed,
    active_version: str | None = "v1",
) -> DocumentRecord:
    return DocumentRecord(
        id=doc_id,
        title=title,
        version="v1",
        s3_uri=f"s3://bucket/{doc_id}",
        status=status,
        owner=owner,
        active_version=active_version,
    )


SAMPLE_DOCS = [
    _doc("doc-1", "Alpha Report", owner="alice@example.com"),
    _doc("doc-2", "Beta Manual", owner="bob@example.com"),
    _doc("doc-3", "Gamma Guide", owner="alice@example.com"),
]


class FakeSettings:
    """Minimal settings stub for the corpus endpoint."""

    auth_enabled = False
    pagination_signing_key = "test-secret"
    corpus_page_size = 50
    operator_emails_set: set[str] = set()


@pytest.fixture()
def client_as_operator(monkeypatch):
    """Return a TestClient where the authenticated user is an operator."""
    monkeypatch.setattr(api_module, "get_settings", lambda: FakeSettings())
    api_module.app.dependency_overrides[get_current_user] = lambda: _OPERATOR_USER
    client = TestClient(api_module.app)
    yield client
    api_module.app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def client_as_non_operator(monkeypatch):
    """Return a TestClient where the authenticated user is a non-operator (alice)."""
    monkeypatch.setattr(api_module, "get_settings", lambda: FakeSettings())
    api_module.app.dependency_overrides[get_current_user] = lambda: _NORMAL_USER
    client = TestClient(api_module.app)
    yield client
    api_module.app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Basic endpoint shape (R4.1)
# ---------------------------------------------------------------------------


def test_corpus_endpoint_returns_200_with_corpus_page(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus")
    assert resp.status_code == 200
    body = resp.json()
    assert "documents" in body
    assert "next_cursor" in body


def test_corpus_endpoint_returns_documents(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus")
    body = resp.json()
    assert len(body["documents"]) == 3
    titles = {d["title"] for d in body["documents"]}
    assert titles == {"Alpha Report", "Beta Manual", "Gamma Guide"}


def test_corpus_endpoint_includes_owner_per_document(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus")
    body = resp.json()
    owners = {d["id"]: d["owner"] for d in body["documents"]}
    assert owners["doc-1"] == "alice@example.com"
    assert owners["doc-2"] == "bob@example.com"


# ---------------------------------------------------------------------------
# Role-based scoping (R4.2, R4.3)
# ---------------------------------------------------------------------------


def test_operator_sees_all_documents(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus")
    body = resp.json()
    assert len(body["documents"]) == 3


def test_non_operator_sees_only_owned_documents(client_as_non_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_non_operator.get("/corpus")
    body = resp.json()
    # alice@example.com owns doc-1 and doc-3
    ids = {d["id"] for d in body["documents"]}
    assert ids == {"doc-1", "doc-3"}


# ---------------------------------------------------------------------------
# Sort/filter/search query params
# ---------------------------------------------------------------------------


def test_sort_by_name_descending(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus", params={"sort_field": "name", "sort_direction": "desc"})
    body = resp.json()
    titles = [d["title"] for d in body["documents"]]
    assert titles == ["Gamma Guide", "Beta Manual", "Alpha Report"]


def test_filter_by_status(client_as_operator, monkeypatch):
    docs = [
        _doc("1", "Indexed", status=DocumentStatus.indexed),
        _doc("2", "Failed", status=DocumentStatus.failed),
    ]
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(docs))
    resp = client_as_operator.get("/corpus", params={"status": "failed"})
    body = resp.json()
    assert len(body["documents"]) == 1
    assert body["documents"][0]["id"] == "2"


def test_search_matches_case_insensitively(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus", params={"search": "beta"})
    body = resp.json()
    assert len(body["documents"]) == 1
    assert body["documents"][0]["id"] == "doc-2"


# ---------------------------------------------------------------------------
# Error: invalid_cursor (R4.6)
# ---------------------------------------------------------------------------


def test_invalid_cursor_returns_400(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus", params={"cursor": "not-a-valid-cursor"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_cursor"


def test_tampered_cursor_returns_400(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    # A cursor signed with a different key
    from rag_system.corpus import SortDirection, SortField, encode_cursor

    forged = encode_cursor(
        sort_field=SortField.name,
        sort_direction=SortDirection.asc,
        sort_value="alpha report",
        last_id="doc-1",
        signing_key="wrong-key",
    )
    resp = client_as_operator.get("/corpus", params={"cursor": forged})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_cursor"


# ---------------------------------------------------------------------------
# Error: search_term_too_long (R4.14)
# ---------------------------------------------------------------------------


def test_search_term_over_200_chars_returns_400(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    long_search = "x" * 201
    resp = client_as_operator.get("/corpus", params={"search": long_search})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "search_term_too_long"


def test_search_term_at_200_chars_is_accepted(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    search_term = "x" * 200
    resp = client_as_operator.get("/corpus", params={"search": search_term})
    # Accepted (no match, but not rejected)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_returns_next_cursor_when_more_exist(client_as_operator, monkeypatch):
    settings = FakeSettings()
    settings.corpus_page_size = 2
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus", params={"page_size": 2})
    body = resp.json()
    assert len(body["documents"]) == 2
    assert body["next_cursor"] is not None


def test_final_page_has_null_next_cursor(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus")
    body = resp.json()
    # All 3 docs fit in page_size=50
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# Invalid sort/direction params
# ---------------------------------------------------------------------------


def test_invalid_sort_field_returns_400(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus", params={"sort_field": "nonexistent"})
    assert resp.status_code == 400
    assert "sort_field" in resp.json()["detail"].lower()


def test_invalid_sort_direction_returns_400(client_as_operator, monkeypatch):
    monkeypatch.setattr(api_module, "get_service", lambda: _fake_service(SAMPLE_DOCS))
    resp = client_as_operator.get("/corpus", params={"sort_direction": "random"})
    assert resp.status_code == 400
    assert "sort_direction" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeService:
    """Minimal service stub that returns a fixed document list."""

    def __init__(self, documents: list[DocumentRecord]):
        self._documents = documents

    def list_documents(self) -> list[DocumentRecord]:
        return self._documents


def _fake_service(documents: list[DocumentRecord]) -> _FakeService:
    return _FakeService(documents)
