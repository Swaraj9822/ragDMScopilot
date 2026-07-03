"""Unit tests for server-side multi-turn conversation support."""

from __future__ import annotations

from types import SimpleNamespace


from rag_system.conversation import (
    ConversationManager,
    FollowUpRewriter,
    _parse_rewrite_response,
)
from rag_system.models import ConversationRecord, ConversationTurn, UnifiedQueryRequest
from rag_system.storage import conversation_key


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for the S3 artifact store's JSON interface."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"


class StubRewriter:
    """Records calls and returns a scripted standalone question."""

    def __init__(self, result: str | None = None, *, boom: bool = False) -> None:
        self._result = result
        self._boom = boom
        self.calls: list[tuple[str, int]] = []

    def rewrite(self, question: str, prior_turns: list[ConversationTurn]) -> str:
        self.calls.append((question, len(prior_turns)))
        if self._boom:
            raise RuntimeError("llm exploded")
        return self._result if self._result is not None else question


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        conversation_rewrite_enabled=True,
        conversation_max_turns=12,
        conversation_rewrite_window=6,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _manager(store: FakeStore, rewriter: StubRewriter, **settings_overrides):
    return ConversationManager(
        store=store, settings=_settings(**settings_overrides), rewriter=rewriter
    )


def _turn(question: str, answer: str) -> ConversationTurn:
    return ConversationTurn(
        question=question,
        standalone_question=question,
        answer=answer,
        route="rag",
        trace_id="t" * 32,
        asked_at="2024-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Preparing a turn
# ---------------------------------------------------------------------------


def test_new_conversation_is_created_and_id_returned() -> None:
    store = FakeStore()
    manager = _manager(store, StubRewriter())

    prepared = manager.prepare(UnifiedQueryRequest(question="What was revenue?"))

    assert prepared.conversation_id  # a fresh id was minted
    assert prepared.effective_request.question == "What was revenue?"
    # First turn has no history, so no rewrite happens.
    assert prepared.rewritten_question is None


def test_first_turn_does_not_call_rewriter() -> None:
    rewriter = StubRewriter(result="ignored")
    manager = _manager(FakeStore(), rewriter)

    manager.prepare(UnifiedQueryRequest(question="Sales this year?"))

    assert rewriter.calls == []


def test_followup_is_rewritten_into_standalone_query() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        document_ids=None,
        turns=[_turn("What was revenue in Q1?", "Revenue was $10M in Q1.")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    rewriter = StubRewriter(result="What was revenue in the last quarter?")
    manager = _manager(store, rewriter)

    prepared = manager.prepare(
        UnifiedQueryRequest(question="What about last quarter?", conversation_id="conv-1")
    )

    assert rewriter.calls == [("What about last quarter?", 1)]
    assert prepared.effective_request.question == "What was revenue in the last quarter?"
    assert prepared.rewritten_question == "What was revenue in the last quarter?"
    assert prepared.original_question == "What about last quarter?"


def test_rewritten_question_is_none_when_query_unchanged() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        turns=[_turn("Prior question", "Prior answer")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    # Rewriter echoes the question back unchanged.
    manager = _manager(store, StubRewriter(result="Already standalone?"))

    prepared = manager.prepare(
        UnifiedQueryRequest(question="Already standalone?", conversation_id="conv-1")
    )

    assert prepared.effective_request.question == "Already standalone?"
    assert prepared.rewritten_question is None


def test_rewrite_failure_falls_back_to_original_question() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        turns=[_turn("Prior question", "Prior answer")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    manager = _manager(store, StubRewriter(boom=True))

    prepared = manager.prepare(
        UnifiedQueryRequest(question="What about last quarter?", conversation_id="conv-1")
    )

    # Fail-open: the raw question is used and nothing is surfaced as rewritten.
    assert prepared.effective_request.question == "What about last quarter?"
    assert prepared.rewritten_question is None


def test_rewrite_disabled_skips_the_rewriter() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        turns=[_turn("Prior question", "Prior answer")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    rewriter = StubRewriter(result="should not be used")
    manager = _manager(store, rewriter, conversation_rewrite_enabled=False)

    prepared = manager.prepare(
        UnifiedQueryRequest(question="Follow up?", conversation_id="conv-1")
    )

    assert rewriter.calls == []
    assert prepared.effective_request.question == "Follow up?"


# ---------------------------------------------------------------------------
# Document scope preservation
# ---------------------------------------------------------------------------


def test_scope_inherited_when_request_omits_document_ids() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        document_ids=["doc-a", "doc-b"],
        turns=[_turn("Prior", "Answer")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    manager = _manager(store, StubRewriter())

    prepared = manager.prepare(
        UnifiedQueryRequest(question="Follow up?", conversation_id="conv-1")
    )

    assert prepared.effective_request.document_ids == ["doc-a", "doc-b"]


def test_explicit_scope_overrides_inherited_scope() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        document_ids=["doc-a"],
        turns=[_turn("Prior", "Answer")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    manager = _manager(store, StubRewriter())

    prepared = manager.prepare(
        UnifiedQueryRequest(
            question="Follow up?", conversation_id="conv-1", document_ids=["doc-z"]
        )
    )

    assert prepared.effective_request.document_ids == ["doc-z"]


# ---------------------------------------------------------------------------
# Forget context
# ---------------------------------------------------------------------------


def test_forget_context_ignores_prior_turns_for_rewrite() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        document_ids=["doc-a"],
        turns=[_turn("Prior", "Answer")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    rewriter = StubRewriter(result="should not be used")
    manager = _manager(store, rewriter)

    prepared = manager.prepare(
        UnifiedQueryRequest(
            question="Fresh start?", conversation_id="conv-1", forget_context=True
        )
    )

    assert rewriter.calls == []  # no history means no rewrite
    assert prepared.effective_request.question == "Fresh start?"
    # Scope is still preserved even when context is forgotten.
    assert prepared.effective_request.document_ids == ["doc-a"]


def test_record_turn_with_forget_clears_prior_history() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        turns=[_turn("Old q", "Old a")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    manager = _manager(store, StubRewriter())

    prepared = manager.prepare(
        UnifiedQueryRequest(
            question="Fresh?", conversation_id="conv-1", forget_context=True
        )
    )
    manager.record_turn(prepared, answer="Fresh answer", route="rag", trace_id="t" * 32)

    stored = ConversationRecord.model_validate(store.objects[conversation_key("conv-1")])
    assert len(stored.turns) == 1
    assert stored.turns[0].question == "Fresh?"


def test_forget_endpoint_clears_turns_but_keeps_scope() -> None:
    store = FakeStore()
    conv = ConversationRecord(
        conversation_id="conv-1",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        document_ids=["doc-a"],
        turns=[_turn("q1", "a1"), _turn("q2", "a2")],
    )
    store.objects[conversation_key("conv-1")] = conv.model_dump(mode="json")
    manager = _manager(store, StubRewriter())

    result = manager.forget("conv-1")

    assert result is not None
    assert result.turns == []
    assert result.document_ids == ["doc-a"]


def test_forget_missing_conversation_returns_none() -> None:
    manager = _manager(FakeStore(), StubRewriter())
    assert manager.forget("nope") is None


# ---------------------------------------------------------------------------
# Recording + persistence
# ---------------------------------------------------------------------------


def test_record_turn_persists_and_updates_scope() -> None:
    store = FakeStore()
    manager = _manager(store, StubRewriter())
    prepared = manager.prepare(
        UnifiedQueryRequest(question="First?", document_ids=["doc-a"])
    )

    manager.record_turn(prepared, answer="An answer", route="rag", trace_id="t" * 32)

    key = conversation_key(prepared.conversation_id)
    assert key in store.objects
    stored = ConversationRecord.model_validate(store.objects[key])
    assert len(stored.turns) == 1
    assert stored.turns[0].question == "First?"
    assert stored.turns[0].answer == "An answer"
    assert stored.document_ids == ["doc-a"]


def test_turns_are_capped_at_max_turns() -> None:
    store = FakeStore()
    manager = _manager(store, StubRewriter(), conversation_max_turns=3)
    conv_id: str | None = None

    for i in range(5):
        prepared = manager.prepare(
            UnifiedQueryRequest(question=f"Q{i}", conversation_id=conv_id)
        )
        conv_id = prepared.conversation_id
        manager.record_turn(prepared, answer=f"A{i}", route="rag", trace_id="t" * 32)

    stored = ConversationRecord.model_validate(store.objects[conversation_key(conv_id)])
    assert len(stored.turns) == 3
    # The three most recent turns are retained.
    assert [t.question for t in stored.turns] == ["Q2", "Q3", "Q4"]


def test_corrupt_record_is_treated_as_new() -> None:
    store = FakeStore()
    store.objects[conversation_key("conv-1")] = {"unexpected": "shape"}
    manager = _manager(store, StubRewriter())

    prepared = manager.prepare(
        UnifiedQueryRequest(question="Hello?", conversation_id="conv-1")
    )

    # A brand-new record replaces the corrupt one; the id is preserved.
    assert prepared.conversation_id == "conv-1"
    assert prepared.effective_request.question == "Hello?"


# ---------------------------------------------------------------------------
# Rewrite response parsing
# ---------------------------------------------------------------------------


def test_parse_plain_json_rewrite() -> None:
    raw = '{"standalone_question": "What was total revenue in Q2?"}'
    assert _parse_rewrite_response(raw, fallback="orig") == "What was total revenue in Q2?"


def test_parse_fenced_json_rewrite() -> None:
    raw = '```json\n{"standalone_question": "Rewritten question?"}\n```'
    assert _parse_rewrite_response(raw, fallback="orig") == "Rewritten question?"


def test_parse_invalid_rewrite_falls_back() -> None:
    assert _parse_rewrite_response("not json at all", fallback="orig") == "orig"


def test_parse_empty_rewrite_falls_back() -> None:
    assert _parse_rewrite_response("", fallback="orig") == "orig"


def test_parse_missing_field_falls_back() -> None:
    assert _parse_rewrite_response('{"other": "x"}', fallback="orig") == "orig"


# ---------------------------------------------------------------------------
# Rewriter windowing (uses a fake LLM to avoid any network)
# ---------------------------------------------------------------------------


class RecordingLLM:
    model_id = "fake-llm"

    def __init__(self) -> None:
        self.last_prompt: str | None = None

    def generate(self, prompt: str, *, temperature: float, max_tokens: int):
        self.last_prompt = prompt
        return '{"standalone_question": "rewritten"}', {}


def test_rewriter_only_includes_recent_window() -> None:
    llm = RecordingLLM()
    rewriter = FollowUpRewriter(_settings(conversation_rewrite_window=2), llm=llm)
    turns = [_turn(f"Q{i}", f"A{i}") for i in range(5)]

    result = rewriter.rewrite("Follow up?", turns)

    assert result == "rewritten"
    # Only the last two prior questions should appear in the prompt.
    assert "Q4" in llm.last_prompt
    assert "Q3" in llm.last_prompt
    assert "Q0" not in llm.last_prompt


def test_rewriter_no_history_returns_question_verbatim() -> None:
    llm = RecordingLLM()
    rewriter = FollowUpRewriter(_settings(), llm=llm)

    assert rewriter.rewrite("Standalone?", []) == "Standalone?"
    assert llm.last_prompt is None  # LLM never called without history
