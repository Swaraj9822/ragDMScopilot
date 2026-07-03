"""Router-level tests for multi-turn conversation wiring.

Exercises :class:`AgenticRouter` with an injected conversation manager to prove
that a conversation id and rewritten query flow through both the non-streaming
and streaming entry points, and that document scope carries across turns.
"""

from __future__ import annotations

from types import SimpleNamespace

from rag_system.conversation import ConversationManager
from rag_system.models import (
    ConversationRecord,
    QueryRequest,
    QueryResponse,
    UnifiedQueryRequest,
)
from rag_system.router import AgenticRouter, QueryRoute, RoutingDecision
from rag_system.storage import conversation_key


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"


class StubRewriter:
    def __init__(self, result: str) -> None:
        self._result = result
        self.calls = 0

    def rewrite(self, question: str, prior_turns) -> str:
        self.calls += 1
        return self._result


class FakeClassifier:
    def classify(self, question: str, tables) -> RoutingDecision:
        return RoutingDecision(route=QueryRoute.rag, reasoning="docs", confidence=0.9)


class FakeRag:
    """Records the QueryRequest it received and returns a canned answer."""

    def __init__(self) -> None:
        self.requests: list[QueryRequest] = []

    def query(self, request: QueryRequest) -> QueryResponse:
        self.requests.append(request)
        return QueryResponse(
            answer=f"Answer to: {request.question}",
            citations=[],
            evidence_status="grounded",
            trace_id="a" * 32,
            confidence_score=0.8,
            retrieval_scores=[0.9],
        )

    def query_stream(self, request: QueryRequest):
        self.requests.append(request)
        yield {"type": "status", "stage": "retrieving"}
        yield {"type": "delta", "text": "Answer"}
        yield {
            "type": "final",
            "response": QueryResponse(
                answer=f"Answer to: {request.question}",
                citations=[],
                evidence_status="grounded",
                trace_id="a" * 32,
                confidence_score=0.8,
                retrieval_scores=[0.9],
            ),
        }


def _router(store: FakeStore, rewriter: StubRewriter) -> AgenticRouter:
    settings = SimpleNamespace(
        conversation_rewrite_enabled=True,
        conversation_max_turns=12,
        conversation_rewrite_window=6,
        hybrid_synthesis_mode="auto",
        hybrid_overlap_threshold=0.12,
    )
    manager = ConversationManager(store=store, settings=settings, rewriter=rewriter)
    router = object.__new__(AgenticRouter)
    router._settings = settings
    router._classifier = FakeClassifier()
    router._rag = FakeRag()
    router._copilot = None
    router._copilot_available = False
    router._table_names = []
    router._conversations = manager
    return router


def test_query_returns_conversation_id_and_records_turn() -> None:
    store = FakeStore()
    router = _router(store, StubRewriter("unused"))

    response = router.query(UnifiedQueryRequest(question="What was revenue?"))

    assert response.conversation_id  # a new conversation was minted
    assert response.rewritten_question is None  # first turn, no rewrite
    stored = ConversationRecord.model_validate(
        store.objects[conversation_key(response.conversation_id)]
    )
    assert len(stored.turns) == 1
    assert stored.turns[0].question == "What was revenue?"


def test_followup_query_is_rewritten_and_scope_preserved() -> None:
    store = FakeStore()
    rewriter = StubRewriter("What was revenue last quarter?")
    router = _router(store, rewriter)

    # First turn establishes a document scope.
    first = router.query(
        UnifiedQueryRequest(question="What was revenue?", document_ids=["doc-a"])
    )
    conv_id = first.conversation_id

    # Follow-up omits document_ids — scope must be inherited, and the follow-up
    # rewritten into a standalone query that retrieval actually sees.
    second = router.query(
        UnifiedQueryRequest(question="What about last quarter?", conversation_id=conv_id)
    )

    assert rewriter.calls == 1
    assert second.rewritten_question == "What was revenue last quarter?"
    # The RAG layer saw the rewritten question and the inherited scope.
    last_request = router._rag.requests[-1]
    assert last_request.question == "What was revenue last quarter?"
    assert last_request.document_ids == ["doc-a"]


def test_query_stream_emits_conversation_meta_and_final() -> None:
    store = FakeStore()
    rewriter = StubRewriter("What was revenue last quarter?")
    router = _router(store, rewriter)

    # Seed a first turn so the follow-up gets rewritten.
    router.query(UnifiedQueryRequest(question="What was revenue?", document_ids=["doc-a"]))
    conv_id = ConversationRecord.model_validate(
        next(iter(store.objects.values()))
    ).conversation_id

    events = list(
        router.query_stream(
            UnifiedQueryRequest(
                question="What about last quarter?", conversation_id=conv_id
            )
        )
    )

    # The new streaming protocol emits stage-progress (status) events and one
    # terminal event carrying the answer payload with conversation metadata.
    terminal = next(e for e in events if e.get("type") == "terminal")
    assert terminal["conversation_id"] == conv_id
    assert terminal["kind"] == "answer"
    assert terminal["payload"]["rewritten_question"] == "What was revenue last quarter?"

    # The turn was recorded (first + second turn persisted).
    stored = ConversationRecord.model_validate(store.objects[conversation_key(conv_id)])
    assert len(stored.turns) == 2
    assert stored.turns[-1].standalone_question == "What was revenue last quarter?"
