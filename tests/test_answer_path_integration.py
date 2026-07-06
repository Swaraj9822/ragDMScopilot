"""Integration tests for the unified answer path wiring (task 5.1).

Exercises the full decision flow through the router's /ask (query), /ask/stream
(query_stream), and /ask/clarify paths against fakes, validating:
- Answer with claims is returned when all gates pass (R1.14).
- Clarification prompt is returned when ambiguous (R2.1).
- Abstention response is returned when a gate fires (R3.1–R3.6).
- /ask/stream emits stage-progress events and one terminal event (no deltas).
- POST /ask/clarify returns answer or rejects invalid/expired/empty replies.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag_system.clarification import (
    ClarificationInvalidOrExpiredError,
    ClarificationReplyProcessor,
    ClarificationReplyRequiredError,
    ClarificationStore,
)
from rag_system.models import (
    AbstentionResponse,
    AnswerSpan,
    Claim,
    ClarificationPrompt,
    EvidenceStatus,
    QueryRequest,
    QueryResponse,
    ReasonCode,
    UnifiedQueryRequest,
    UnifiedQueryResponse,
)
from rag_system.router import AgenticRouter, QueryRoute, RoutingDecision


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory fake for GcsArtifactStore / JsonStore."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"

    def create_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"


def _settings(**overrides):
    defaults = dict(
        conversation_rewrite_enabled=False,
        conversation_max_turns=12,
        conversation_rewrite_window=6,
        hybrid_synthesis_mode="auto",
        hybrid_overlap_threshold=0.12,
        route_min_confidence=0.5,
        retrieval_score_threshold=0.3,
        clarification_expiry_minutes=30,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _AmbiguousClassifier:
    """Classifier that always returns ambiguous."""

    def classify(self, question: str, tables) -> RoutingDecision:
        return RoutingDecision(
            route=QueryRoute.rag,
            reasoning="ambiguous",
            confidence=0.4,
            ambiguous=True,
            clarification_question="Could you clarify what you mean?",
        )


class _NormalClassifier:
    """Classifier that routes to RAG without ambiguity."""

    def classify(self, question: str, tables) -> RoutingDecision:
        return RoutingDecision(route=QueryRoute.rag, reasoning="docs", confidence=0.9)


class _FakeRag:
    """Returns a canned answer with configurable claims and confidence."""

    def __init__(
        self,
        confidence_score: float = 0.8,
        claims: list[Claim] | None = None,
        retrieval_scores: list[float] | None = None,
    ) -> None:
        self._confidence_score = confidence_score
        self._claims = claims or []
        # A grounded answer implies retrieval returned hits; default to a single
        # above-threshold score so the retrieval abstention gates (R3.2/R3.6) do
        # not fire for the happy path. Tests that exercise those gates pass an
        # empty list (zero hits) or all-low scores explicitly.
        self._retrieval_scores = (
            [0.9] if retrieval_scores is None else retrieval_scores
        )

    def query(self, request: QueryRequest) -> QueryResponse:
        return QueryResponse(
            answer=f"Answer to: {request.question}",
            citations=[],
            evidence_status="grounded",
            trace_id="trace-123",
            confidence_score=self._confidence_score,
            claims=self._claims,
            retrieval_scores=self._retrieval_scores,
        )

    def query_stream(self, request: QueryRequest):
        yield {"type": "status", "stage": "retrieving"}
        yield {
            "type": "final",
            "response": self.query(request),
        }


def _build_router(
    classifier=None,
    rag=None,
    settings=None,
    clarification_store=None,
) -> AgenticRouter:
    """Build an AgenticRouter with injected fakes."""
    settings = settings or _settings()
    router = object.__new__(AgenticRouter)
    router._settings = settings
    router._classifier = classifier or _NormalClassifier()
    router._rag = rag or _FakeRag()
    router._copilot = None
    router._copilot_available = False
    router._table_names = []
    router._conversations = None
    router._clarifications = clarification_store
    return router


# ---------------------------------------------------------------------------
# /ask — answer with claims (all gates pass)
# ---------------------------------------------------------------------------


def test_ask_returns_answer_with_claims_when_all_gates_pass():
    """When the confidence is above threshold and no abstention triggers fire,
    the endpoint returns a UnifiedQueryResponse with claims."""
    claims = [
        Claim(
            claim_id="c1",
            text="Revenue was $10M",
            answer_span=AnswerSpan(start=0, end=16),
            evidence_items=[],
            evidence_status=EvidenceStatus.supported,
        )
    ]
    router = _build_router(rag=_FakeRag(confidence_score=0.8, claims=claims))
    result = router.query(UnifiedQueryRequest(question="What was revenue?"))

    assert isinstance(result, UnifiedQueryResponse)
    assert result.answer.startswith("Answer to:")
    assert len(result.claims) == 1
    assert result.claims[0].claim_id == "c1"


# ---------------------------------------------------------------------------
# /ask — clarification prompt (ambiguous)
# ---------------------------------------------------------------------------


def test_ask_returns_clarification_when_ambiguous():
    """When the classifier flags ambiguity, a ClarificationPrompt is returned
    instead of an answer (R2.1)."""
    store = FakeStore()
    settings = _settings()
    clarification_store = ClarificationStore(store, settings)
    router = _build_router(
        classifier=_AmbiguousClassifier(),
        clarification_store=clarification_store,
        settings=settings,
    )
    result = router.query(UnifiedQueryRequest(question="Tell me about it"))

    assert isinstance(result, ClarificationPrompt)
    assert result.clarification_question == "Could you clarify what you mean?"
    assert result.clarification_id  # non-empty unguessable token


# ---------------------------------------------------------------------------
# /ask — abstention (low confidence gate fires)
# ---------------------------------------------------------------------------


def test_ask_returns_abstention_when_confidence_below_threshold():
    """When confidence is below route_min_confidence, an AbstentionResponse is
    returned (R3.1)."""
    router = _build_router(
        rag=_FakeRag(confidence_score=0.2),
        settings=_settings(route_min_confidence=0.5),
    )
    result = router.query(UnifiedQueryRequest(question="What?"))

    assert isinstance(result, AbstentionResponse)
    assert result.reason_code == ReasonCode.low_confidence
    assert result.missing_information  # non-empty description


def test_ask_returns_abstention_when_retrieval_returned_no_hits():
    """A RAG answer whose retrieval returned zero hits abstains with
    ``no_evidence`` (R3.2)."""
    router = _build_router(
        rag=_FakeRag(confidence_score=0.8, retrieval_scores=[]),
        settings=_settings(),
    )
    result = router.query(UnifiedQueryRequest(question="What?"))

    assert isinstance(result, AbstentionResponse)
    assert result.reason_code == ReasonCode.no_evidence


def test_ask_returns_abstention_when_all_hits_below_threshold():
    """A RAG answer whose retrieval hits are all below the score threshold
    abstains with ``retrieval_below_threshold`` (R3.6)."""
    router = _build_router(
        rag=_FakeRag(confidence_score=0.8, retrieval_scores=[0.1, 0.2]),
        settings=_settings(retrieval_score_threshold=0.3),
    )
    result = router.query(UnifiedQueryRequest(question="What?"))

    assert isinstance(result, AbstentionResponse)
    assert result.reason_code == ReasonCode.retrieval_below_threshold


# ---------------------------------------------------------------------------
# /ask/stream — emits stage-progress events and one terminal event
# ---------------------------------------------------------------------------


def test_stream_emits_stages_and_terminal_answer():
    """The stream emits status events for pipeline stages and exactly one
    terminal event with the answer payload. No delta events are emitted."""
    router = _build_router(rag=_FakeRag(confidence_score=0.8))
    events = list(
        router.query_stream(UnifiedQueryRequest(question="What was revenue?"))
    )

    status_events = [e for e in events if e.get("type") == "status"]
    terminal_events = [e for e in events if e.get("type") == "terminal"]
    delta_events = [e for e in events if e.get("type") == "delta"]

    # Stage progress events for liveness
    stages = [e["stage"] for e in status_events]
    assert "classify" in stages
    assert "retrieve" in stages
    assert "generate" in stages
    assert "verify" in stages

    # Exactly one terminal event
    assert len(terminal_events) == 1
    terminal = terminal_events[0]
    assert terminal["kind"] == "answer"
    assert "payload" in terminal
    assert terminal["payload"]["answer"].startswith("Answer to:")

    # No delta events (answer content is held)
    assert delta_events == []


def test_stream_terminal_clarification_when_ambiguous():
    """When ambiguous, the stream emits a terminal clarification event
    instead of an answer."""
    store = FakeStore()
    settings = _settings()
    clarification_store = ClarificationStore(store, settings)
    router = _build_router(
        classifier=_AmbiguousClassifier(),
        clarification_store=clarification_store,
        settings=settings,
    )
    events = list(
        router.query_stream(UnifiedQueryRequest(question="Tell me about it"))
    )

    terminal_events = [e for e in events if e.get("type") == "terminal"]
    assert len(terminal_events) == 1
    terminal = terminal_events[0]
    assert terminal["kind"] == "clarification"
    assert terminal["payload"]["clarification_question"] == "Could you clarify what you mean?"


def test_stream_terminal_abstention_when_low_confidence():
    """When confidence triggers abstention, the stream emits a terminal
    abstention event with no answer content."""
    router = _build_router(
        rag=_FakeRag(confidence_score=0.2),
        settings=_settings(route_min_confidence=0.5),
    )
    events = list(
        router.query_stream(UnifiedQueryRequest(question="What?"))
    )

    terminal_events = [e for e in events if e.get("type") == "terminal"]
    assert len(terminal_events) == 1
    terminal = terminal_events[0]
    assert terminal["kind"] == "abstention"
    assert terminal["payload"]["reason_code"] == "low_confidence"
    # No answer content in the terminal event
    assert "answer" not in terminal["payload"]


# ---------------------------------------------------------------------------
# POST /ask/clarify — happy path
# ---------------------------------------------------------------------------


def test_clarify_returns_answer_on_valid_reply():
    """A valid reply to a live clarification re-runs the answer path and
    returns a response (R2.4)."""
    store = FakeStore()
    settings = _settings()
    clarification_store = ClarificationStore(store, settings)

    # Issue a clarification to create a record.
    prompt = clarification_store.issue(
        original_question="Tell me about it",
        conversation_turn_id="conv-1:0",
        clarification_question="Could you clarify?",
        document_scope=["doc-a"],
    )
    assert prompt is not None

    # Build a processor with an answer path that returns a normal answer.
    def answer_path(*, question: str, document_scope: list[str] | None):
        return UnifiedQueryResponse(
            answer=f"Answered: {question}",
            route="rag",
            evidence_status="grounded",
            trace_id="t-1",
            confidence_score=0.9,
        )

    processor = ClarificationReplyProcessor(
        store=clarification_store, answer_path=answer_path
    )
    outcome = processor.process(
        clarification_id=prompt.clarification_id, reply="I meant revenue"
    )

    assert isinstance(outcome, UnifiedQueryResponse)
    assert "revenue" in outcome.answer.lower() or "tell me" in outcome.answer.lower()


# ---------------------------------------------------------------------------
# POST /ask/clarify — invalid/expired id
# ---------------------------------------------------------------------------


def test_clarify_rejects_invalid_id():
    """An unknown clarification_id raises ClarificationInvalidOrExpiredError
    (R2.5)."""
    store = FakeStore()
    settings = _settings()
    clarification_store = ClarificationStore(store, settings)

    def answer_path(*, question: str, document_scope: list[str] | None):
        return UnifiedQueryResponse(
            answer="x",
            route="rag",
            evidence_status="grounded",
            trace_id="t",
            confidence_score=0.9,
        )

    processor = ClarificationReplyProcessor(
        store=clarification_store, answer_path=answer_path
    )
    with pytest.raises(ClarificationInvalidOrExpiredError):
        processor.process(clarification_id="nonexistent-id", reply="yes")


# ---------------------------------------------------------------------------
# POST /ask/clarify — empty reply
# ---------------------------------------------------------------------------


def test_clarify_rejects_empty_reply():
    """An empty or whitespace-only reply raises ClarificationReplyRequiredError
    (R2.6)."""
    store = FakeStore()
    settings = _settings()
    clarification_store = ClarificationStore(store, settings)

    prompt = clarification_store.issue(
        original_question="What?",
        conversation_turn_id="conv-1:0",
        clarification_question="Clarify?",
        document_scope=None,
    )

    def answer_path(*, question: str, document_scope: list[str] | None):
        return UnifiedQueryResponse(
            answer="x",
            route="rag",
            evidence_status="grounded",
            trace_id="t",
            confidence_score=0.9,
        )

    processor = ClarificationReplyProcessor(
        store=clarification_store, answer_path=answer_path
    )
    with pytest.raises(ClarificationReplyRequiredError):
        processor.process(clarification_id=prompt.clarification_id, reply="   ")
