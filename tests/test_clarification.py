"""Unit tests for ambiguity clarification issuance (R2.1, R2.2, R2.9).

Covers the classifier parsing the ambiguity signals, the pure question-selection
helper, and the create-only persistence + prompt shape of ``ClarificationStore``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from rag_system.clarification import (
    DEFAULT_CLARIFICATION_QUESTION,
    SCOPE_CLARIFICATION_QUESTION,
    ClarificationInvalidOrExpiredError,
    ClarificationReplyProcessor,
    ClarificationReplyRequiredError,
    ClarificationStore,
    combine_question_and_reply,
    resolve_clarification_question,
)
from rag_system.models import (
    AbstentionResponse,
    ClarificationPrompt,
    ClarificationRecord,
    ReasonCode,
    UnifiedQueryResponse,
)
from rag_system.router import QueryRoute, _parse_routing_response
from rag_system.storage import clarification_key


class _FakeStore:
    """Records create-only writes (rejecting duplicates) and serves reads."""

    def __init__(self) -> None:
        self.writes: dict[str, object] = {}

    def create_json(self, key: str, payload: object) -> str:
        if key in self.writes:
            raise AssertionError(f"duplicate create for {key}")
        self.writes[key] = payload
        return f"s3://fake/{key}"

    def get_json(self, key: str) -> object | None:
        return self.writes.get(key)


def _settings(expiry_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(clarification_expiry_minutes=expiry_minutes)


# ---------------------------------------------------------------------------
# Classifier parsing of ambiguity signals
# ---------------------------------------------------------------------------


def test_parse_ambiguous_flag_and_question() -> None:
    raw = (
        '{"route": "rag", "reasoning": "under-specified", "confidence": 0.4, '
        '"ambiguous": true, "clarification_question": "Which product do you mean?"}'
    )
    decision = _parse_routing_response(raw)
    assert decision.ambiguous is True
    assert decision.scope_ambiguous is False
    assert decision.clarification_question == "Which product do you mean?"


def test_parse_scope_ambiguous_implies_ambiguous() -> None:
    raw = (
        '{"route": "rag", "reasoning": "unclear scope", "confidence": 0.5, '
        '"ambiguous": false, "scope_ambiguous": true, "clarification_question": null}'
    )
    decision = _parse_routing_response(raw)
    # scope ambiguity is a kind of ambiguity even if the broader flag was false.
    assert decision.scope_ambiguous is True
    assert decision.ambiguous is True
    assert decision.clarification_question is None


def test_parse_defaults_to_unambiguous() -> None:
    raw = '{"route": "database", "reasoning": "metrics", "confidence": 0.9}'
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.database
    assert decision.ambiguous is False
    assert decision.scope_ambiguous is False
    assert decision.clarification_question is None


# ---------------------------------------------------------------------------
# Question selection
# ---------------------------------------------------------------------------


def test_scope_ambiguity_uses_selected_vs_corpus_wording() -> None:
    question = resolve_clarification_question(
        scope_ambiguous=True, clarification_question="ignored when scope-ambiguous"
    )
    assert question == SCOPE_CLARIFICATION_QUESTION
    assert "selected" in question.lower()
    assert "corpus" in question.lower()


def test_uses_classifier_question_when_present() -> None:
    assert (
        resolve_clarification_question(
            scope_ambiguous=False, clarification_question="  Which year?  "
        )
        == "Which year?"
    )


def test_falls_back_to_default_question_when_missing() -> None:
    assert (
        resolve_clarification_question(scope_ambiguous=False, clarification_question=None)
        == DEFAULT_CLARIFICATION_QUESTION
    )
    assert (
        resolve_clarification_question(scope_ambiguous=False, clarification_question="   ")
        == DEFAULT_CLARIFICATION_QUESTION
    )


# ---------------------------------------------------------------------------
# ClarificationStore persistence + prompt shape
# ---------------------------------------------------------------------------


def test_issue_persists_record_and_returns_matching_prompt() -> None:
    store = _FakeStore()
    clarifications = ClarificationStore(store, _settings(expiry_minutes=30))

    prompt = clarifications.issue(
        original_question="What was revenue?",
        conversation_turn_id="conv-1:0",
        clarification_question="Which fiscal year?",
        document_scope=["doc-a", "doc-b"],
    )

    # A record was written create-only under the id-derived key.
    key = clarification_key(prompt.clarification_id)
    assert key in store.writes
    record = ClarificationRecord.model_validate(store.writes[key])

    # The persisted record binds id -> turn, scope, original question, expiry.
    assert record.clarification_id == prompt.clarification_id
    assert record.conversation_turn_id == "conv-1:0"
    assert record.original_question == "What was revenue?"
    assert record.document_scope == ["doc-a", "doc-b"]
    assert record.clarification_expiry == prompt.clarification_expiry

    # The prompt echoes the same binding for the caller (R2.2).
    assert prompt.clarification_question == "Which fiscal year?"
    assert prompt.conversation_turn_id == "conv-1:0"
    assert prompt.document_scope == ["doc-a", "doc-b"]

    # Expiry is a valid future ISO-8601 timestamp.
    expiry = datetime.fromisoformat(prompt.clarification_expiry)
    assert expiry.tzinfo is not None


def test_issued_ids_are_unguessable_and_unique() -> None:
    store = _FakeStore()
    clarifications = ClarificationStore(store, _settings())

    ids = {
        clarifications.issue(
            original_question="q",
            conversation_turn_id=f"conv:{i}",
            clarification_question="which one?",
        ).clarification_id
        for i in range(10)
    }
    # All distinct and long enough to be unguessable (token_urlsafe(32) ~ 43 chars).
    assert len(ids) == 10
    assert all(len(cid) >= 32 for cid in ids)


def test_expiry_respects_configured_window() -> None:
    store = _FakeStore()
    before = datetime.now().astimezone()
    prompt = ClarificationStore(store, _settings(expiry_minutes=30)).issue(
        original_question="q",
        conversation_turn_id="t",
        clarification_question="which?",
    )
    expiry = datetime.fromisoformat(prompt.clarification_expiry)
    delta_minutes = (expiry - before).total_seconds() / 60
    # ~30 minutes ahead, allowing generous slack for execution time.
    assert 29 <= delta_minutes <= 31


# ---------------------------------------------------------------------------
# Reply processing: record loading / validation (R2.5)
# ---------------------------------------------------------------------------


def _store_record(
    fake: _FakeStore,
    *,
    clarification_id: str = "cid-123",
    original_question: str = "What was revenue?",
    document_scope: list[str] | None = None,
    expiry: datetime | None = None,
) -> ClarificationRecord:
    """Persist a ClarificationRecord directly into the fake store for read tests."""
    if expiry is None:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    record = ClarificationRecord(
        clarification_id=clarification_id,
        conversation_turn_id="conv-1:0",
        original_question=original_question,
        document_scope=document_scope,
        clarification_expiry=expiry.isoformat(),
    )
    fake.writes[clarification_key(clarification_id)] = record.model_dump()
    return record


def test_load_returns_valid_unexpired_record() -> None:
    fake = _FakeStore()
    stored = _store_record(fake, document_scope=["doc-a"])
    store = ClarificationStore(fake, _settings())

    loaded = store.load("cid-123")
    assert loaded.clarification_id == stored.clarification_id
    assert loaded.original_question == "What was revenue?"
    assert loaded.document_scope == ["doc-a"]


def test_load_rejects_unknown_id() -> None:
    store = ClarificationStore(_FakeStore(), _settings())
    with pytest.raises(ClarificationInvalidOrExpiredError) as exc:
        store.load("does-not-exist")
    assert exc.value.code == "clarification_invalid_or_expired"


def test_load_rejects_empty_id() -> None:
    store = ClarificationStore(_FakeStore(), _settings())
    with pytest.raises(ClarificationInvalidOrExpiredError):
        store.load("")


def test_load_rejects_expired_record() -> None:
    fake = _FakeStore()
    _store_record(
        fake, expiry=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    store = ClarificationStore(fake, _settings())
    with pytest.raises(ClarificationInvalidOrExpiredError):
        store.load("cid-123")


def test_load_rejects_corrupt_record() -> None:
    fake = _FakeStore()
    fake.writes[clarification_key("cid-bad")] = {"not": "a valid record"}
    store = ClarificationStore(fake, _settings())
    with pytest.raises(ClarificationInvalidOrExpiredError):
        store.load("cid-bad")


# ---------------------------------------------------------------------------
# Reply processing: combine + orchestration (R2.4, R2.6, R2.7, R2.8)
# ---------------------------------------------------------------------------


def test_combine_question_and_reply_joins_original_and_reply() -> None:
    combined = combine_question_and_reply("  What was revenue?  ", "  FY2023  ")
    assert "What was revenue?" in combined
    assert "FY2023" in combined
    # Whitespace on the ends is trimmed.
    assert combined == "What was revenue?\n\nClarification: FY2023"


class _RecordingAnswerPath:
    """Answer path double that records its call and returns a configured value."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def __call__(self, *, question: str, document_scope: list[str] | None):
        self.calls.append({"question": question, "document_scope": document_scope})
        return self.result


def _answer(text: str = "Revenue was $10M.") -> UnifiedQueryResponse:
    return UnifiedQueryResponse(
        answer=text,
        route="rag",
        evidence_status="grounded",
        trace_id="trace-1",
    )


def test_process_reruns_answer_path_scoped_and_combined() -> None:
    fake = _FakeStore()
    _store_record(fake, original_question="What was revenue?", document_scope=["doc-a", "doc-b"])
    store = ClarificationStore(fake, _settings())
    answer_path = _RecordingAnswerPath(_answer())
    processor = ClarificationReplyProcessor(store, answer_path)

    outcome = processor.process(clarification_id="cid-123", reply="fiscal year 2023")

    # The answer path was invoked exactly once, scoped to the record's scope
    # with the original question combined with the reply (R2.4, R2.7).
    assert len(answer_path.calls) == 1
    call = answer_path.calls[0]
    assert call["document_scope"] == ["doc-a", "doc-b"]
    assert "What was revenue?" in call["question"]  # type: ignore[operator]
    assert "fiscal year 2023" in call["question"]  # type: ignore[operator]
    # The answer flows straight through.
    assert isinstance(outcome, UnifiedQueryResponse)
    assert outcome.answer == "Revenue was $10M."


def test_process_passes_through_abstention() -> None:
    fake = _FakeStore()
    _store_record(fake)
    store = ClarificationStore(fake, _settings())
    abstention = AbstentionResponse(
        reason_code=ReasonCode.no_evidence,
        missing_information="Nothing retrieved.",
        trace_id="trace-9",
    )
    processor = ClarificationReplyProcessor(store, _RecordingAnswerPath(abstention))

    outcome = processor.process(clarification_id="cid-123", reply="more detail")
    assert outcome is abstention


def test_process_rejects_empty_reply() -> None:
    fake = _FakeStore()
    _store_record(fake)
    store = ClarificationStore(fake, _settings())
    answer_path = _RecordingAnswerPath(_answer())
    processor = ClarificationReplyProcessor(store, answer_path)

    with pytest.raises(ClarificationReplyRequiredError) as exc:
        processor.process(clarification_id="cid-123", reply="   ")
    assert exc.value.code == "clarification_reply_required"
    # The answer path must not run for an empty reply (R2.6).
    assert answer_path.calls == []


def test_process_rejects_invalid_id_before_running_answer_path() -> None:
    store = ClarificationStore(_FakeStore(), _settings())
    answer_path = _RecordingAnswerPath(_answer())
    processor = ClarificationReplyProcessor(store, answer_path)

    with pytest.raises(ClarificationInvalidOrExpiredError):
        processor.process(clarification_id="unknown", reply="a reply")
    assert answer_path.calls == []


def test_process_abstains_when_still_ambiguous() -> None:
    """A reply must never yield a further clarification; it abstains (R2.8)."""
    fake = _FakeStore()
    _store_record(fake)
    store = ClarificationStore(fake, _settings())
    residual_prompt = ClarificationPrompt(
        clarification_question="Still which one?",
        clarification_id="cid-next",
        conversation_turn_id="conv-1:1",
        clarification_expiry=(
            datetime.now(timezone.utc) + timedelta(minutes=30)
        ).isoformat(),
    )
    processor = ClarificationReplyProcessor(store, _RecordingAnswerPath(residual_prompt))

    outcome = processor.process(clarification_id="cid-123", reply="still vague")
    assert isinstance(outcome, AbstentionResponse)
    assert outcome.reason_code == ReasonCode.low_confidence
    assert outcome.missing_information


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
