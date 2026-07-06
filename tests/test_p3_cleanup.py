"""Tests for the P3 cleanup changes.

Covers:
- Finding 13: the root catalog is generated from the router (no manual drift).
- Finding 14: the streaming path is referenced through one shared constant.
- Finding 12: query() and query_stream() share one retrieval helper (_retrieve).
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.models import (
    Chunk,
    Citation,
    DocumentRecord,
    DocumentStatus,
    QueryRequest,
    QueryResponse,
    RetrievalHit,
)
from rag_system.service import RagService


# ---------------------------------------------------------------------------
# Finding 13 — root catalog generated from the router.
# ---------------------------------------------------------------------------


def test_root_lists_actual_registered_routes() -> None:
    client = TestClient(api_module.app)
    body = client.get("/").json()

    endpoints = body["endpoints"]
    assert isinstance(endpoints, list)
    # A representative sample of real routes must be present and correctly shaped.
    assert "POST /ask/stream" in endpoints
    assert "GET /health" in endpoints
    assert "POST /query" in endpoints
    assert "GET /documents/{document_id}" in endpoints
    # Generated straight from the router → matches the live route set exactly.
    live = {
        f"{method} {route.path}"
        for route in api_module.app.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set())
        if method not in ("HEAD", "OPTIONS")
    }
    assert set(endpoints) == live


def test_root_endpoint_catalog_is_cached() -> None:
    # Item 6: routes are fixed after startup, so the catalog is computed once
    # and cached rather than rebuilt from app.routes on every request.
    api_module._endpoint_catalog.cache_clear()
    first = api_module._endpoint_catalog()
    second = api_module._endpoint_catalog()
    # Same cached tuple object is returned (not recomputed).
    assert first is second
    assert api_module._endpoint_catalog.cache_info().hits >= 1


# ---------------------------------------------------------------------------
# Finding 14 — one source of truth for the streaming path.
# ---------------------------------------------------------------------------


def test_ask_stream_path_constant_matches_a_registered_route() -> None:
    paths = {route.path for route in api_module.app.routes if isinstance(route, APIRoute)}
    assert api_module._ASK_STREAM_PATH in paths


# ---------------------------------------------------------------------------
# Finding 12 — query_stream flows through the shared _retrieve helper.
# ---------------------------------------------------------------------------


class _Embedder:
    def embed_query(self, question: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _Index:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []

    def search(self, query_vector, top_k, document_ids=None, sparse_vector=None):
        self.search_calls.append({"top_k": top_k, "document_ids": document_ids})
        chunk = Chunk(id="c1", document_id="doc", version="v", text="body")
        return [RetrievalHit(chunk=chunk, score=0.9, source="fake")]


class _Generator:
    def answer_stream(self, question, hits, trace_id):
        yield {"type": "delta", "text": "Answer"}
        yield {
            "type": "final",
            "response": QueryResponse(
                answer="Answer",
                citations=[Citation(document_id=h.chunk.document_id, chunk_id=h.chunk.id) for h in hits],
                evidence_status="grounded",
                trace_id=trace_id,
            ),
        }


def _stream_service() -> tuple[RagService, _Index]:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        retrieval_dense_top_k=10,
        context_top_k=5,
        low_top_score_threshold=None,
    )
    index = _Index()
    service._embedder = _Embedder()
    service._sparse_encoder = None
    service._index = index
    service._generator = _Generator()
    # The retrieval gate consults the document record for its published version;
    # cache an indexed record so the fake hit (version "v") is kept.
    service._documents = {
        "doc": DocumentRecord(
            id="doc",
            title="doc.pdf",
            version="v",
            s3_uri="s3://bucket/doc",
            status=DocumentStatus.indexed,
            active_version="v",
        )
    }
    # Trace persistence is off-path and irrelevant to this test.
    service._persist_query_trace_async = lambda **kwargs: None  # type: ignore[method-assign]
    return service, index


def test_query_stream_uses_retrieval_and_emits_final() -> None:
    service, index = _stream_service()

    events = list(service.query_stream(QueryRequest(question="what?", document_ids=["doc"])))

    stages = [e.get("stage") for e in events if e.get("type") == "status"]
    assert stages == ["retrieving", "generating"]

    # The shared _retrieve ran: index.search was called with the configured top_k.
    assert index.search_calls and index.search_calls[0]["top_k"] == 10
    assert index.search_calls[0]["document_ids"] == ["doc"]

    final = [e for e in events if e.get("type") == "final"]
    assert len(final) == 1
    assert final[0]["response"].answer == "Answer"
    assert any(e.get("type") == "delta" for e in events)
