"""Regression tests: database-route answers persist a query trace (R10, R6).

Before the fix, only the RAG route wrote a ``QueryTraceRecord``. Database-routed
answers had no persisted trace, so:

* the trace investigator's ``POST /traces/{id}/diagnose`` returned
  ``trace_not_found`` for every database answer, and
* ``POST /queries/{id}/feedback`` returned 404 ("This answer isn't saved yet"),
  never succeeding no matter how many times the client retried.

These tests cover :meth:`RagService.persist_unified_query_trace` — the method the
router calls on the database route — proving the trace lands under the same key
the diagnose and feedback endpoints read, so both now succeed.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from rag_system.models import QueryFeedbackRequest, UnifiedQueryResponse
from rag_system.service import RagService
from rag_system.storage import query_trace_key


class FakeStore:
    """In-memory stand-in for the artifact store."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"gs://bucket/{key}"


def _service(store: FakeStore) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        rerank_enabled=False,
        gemini_model_id="gemini-x",
        embedding_model_id="embed-x",
        pinecone_index_name="index",
    )
    service._store = store
    return service


def _database_response(trace_id: str = "db-trace-1") -> UnifiedQueryResponse:
    return UnifiedQueryResponse(
        answer="Acme generated the most revenue.",
        route="database",
        evidence_status="grounded",
        trace_id=trace_id,
        confidence_score=0.86,
        sql="SELECT customer, SUM(total) FROM sales_invoice GROUP BY customer",
        rows=[{"customer": "Acme", "sum": 100}],
    )


def _await_trace(service: RagService, trace_id: str, timeout_s: float = 2.0):
    """Poll for the async trace write to land (mirrors the client's retry)."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        trace = service.get_query_trace(trace_id)
        if trace is not None:
            return trace
        time.sleep(0.02)
    return service.get_query_trace(trace_id)


def test_database_route_trace_is_persisted_and_diagnosable():
    """A database answer's trace is written under the diagnose/feedback key."""
    store = FakeStore()
    service = _service(store)

    service.persist_unified_query_trace(
        question="Which customer generated the most revenue?",
        document_ids=None,
        response=_database_response(),
    )

    trace = _await_trace(service, "db-trace-1")
    assert trace is not None, "database-route trace was never persisted"
    assert query_trace_key("db-trace-1") in store.objects
    assert trace.route == "database"
    assert trace.sql == "SELECT customer, SUM(total) FROM sales_invoice GROUP BY customer"
    assert trace.answer == "Acme generated the most revenue."
    assert trace.evidence_status == "grounded"


def test_feedback_succeeds_after_database_trace_persisted():
    """Feedback attaches to a database answer once its trace is written (no 404)."""
    store = FakeStore()
    service = _service(store)

    service.persist_unified_query_trace(
        question="Which customer generated the most revenue?",
        document_ids=None,
        response=_database_response("db-trace-2"),
    )
    assert _await_trace(service, "db-trace-2") is not None

    record = service.record_query_feedback(
        "db-trace-2", QueryFeedbackRequest(rating=5, comment="Correct.")
    )

    assert record is not None, "feedback returned None (would surface as a 404)"
    assert record.trace_id == "db-trace-2"
    assert record.rating == 5
