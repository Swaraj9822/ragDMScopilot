"""Property-based test for single-clarification-then-abstention (R2.7, R2.8).

# Feature: rag-trust-and-observability, Property 8: At most one clarification, then abstention

This module validates that the system issues at most one clarification per
original question. After that one permitted clarification, if the system still
cannot resolve the ambiguity, it returns an AbstentionResponse (not another
ClarificationPrompt). The abstention response contains no answer content.

**Validates: Requirements 2.7, 2.8**
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.clarification import (
    ClarificationReplyProcessor,
    ClarificationStore,
)
from rag_system.models import (
    AbstentionResponse,
    ClarificationPrompt,
    ClarificationRecord,
    UnifiedQueryResponse,
)
from rag_system.storage import clarification_key


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeStore:
    """Create-only JSON store double replicating GcsArtifactStore semantics."""

    def __init__(self) -> None:
        self.writes: dict[str, object] = {}

    def create_json(self, key: str, payload: object) -> str:
        if key in self.writes:
            raise AssertionError(f"duplicate create for {key}")
        self.writes[key] = payload
        return f"s3://fake/{key}"

    def get_json(self, key: str) -> object | None:
        return self.writes.get(key)


def _cfg(expiry_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(clarification_expiry_minutes=expiry_minutes)


def _seed_record(
    fake: _FakeStore,
    *,
    clarification_id: str,
    original_question: str,
    document_scope: list[str] | None = None,
    expiry_minutes: int = 30,
) -> ClarificationRecord:
    """Persist a valid unexpired ClarificationRecord directly for testing."""
    expiry = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
    record = ClarificationRecord(
        clarification_id=clarification_id,
        conversation_turn_id="conv-1:0",
        original_question=original_question,
        document_scope=document_scope,
        clarification_expiry=expiry.isoformat(),
    )
    fake.writes[clarification_key(clarification_id)] = record.model_dump()
    return record


# ---------------------------------------------------------------------------
# Answer path stubs: control whether the clarification resolves or not
# ---------------------------------------------------------------------------


class _StillAmbiguousAnswerPath:
    """Answer path that always returns another ClarificationPrompt.

    Simulates the scenario where the router still classifies the combined
    question as ambiguous even after the user's clarification reply.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(
        self, *, question: str, document_scope: list[str] | None
    ) -> ClarificationPrompt:
        self.call_count += 1
        return ClarificationPrompt(
            clarification_question="Still unclear — which one?",
            clarification_id="cid-residual",
            conversation_turn_id="conv-1:1",
            clarification_expiry=(
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
            document_scope=document_scope,
        )


class _ResolvingAnswerPath:
    """Answer path that always returns a successful answer."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(
        self, *, question: str, document_scope: list[str] | None
    ) -> UnifiedQueryResponse:
        self.call_count += 1
        return UnifiedQueryResponse(
            answer="The answer is 42.",
            route="rag",
            evidence_status="grounded",
            trace_id="trace-resolved",
        )


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Non-empty, non-whitespace replies (R2.6 rejects empty; our test is about
# what happens AFTER validation passes).
_replies = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")
_original_questions = st.text(min_size=1, max_size=300)
_scopes = st.one_of(
    st.none(),
    st.lists(st.text(min_size=1, max_size=32), min_size=1, max_size=6),
)
_clarification_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=8,
    max_size=64,
)


# ---------------------------------------------------------------------------
# Property 8: At most one clarification, then abstention
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 8: At most one clarification, then abstention
@settings(max_examples=200)
@given(
    original_question=_original_questions,
    reply=_replies,
    document_scope=_scopes,
    clarification_id=_clarification_ids,
)
def test_at_most_one_clarification_then_abstention(
    original_question: str,
    reply: str,
    document_scope: list[str] | None,
    clarification_id: str,
) -> None:
    """After the one permitted clarification, the system abstains — never re-asks.

    This test demonstrates:
    1. The system issues at most one clarification per question (the answer path
       is invoked exactly once per process() call).
    2. After the one permitted clarification, if still unresolved, returns an
       AbstentionResponse (not another ClarificationPrompt).
    3. The abstention response contains no answer content.

    **Validates: Requirements 2.7, 2.8**
    """
    # Seed the store with a valid clarification record — this represents the
    # one permitted clarification that was already issued for the question.
    fake = _FakeStore()
    _seed_record(
        fake,
        clarification_id=clarification_id,
        original_question=original_question,
        document_scope=document_scope,
    )
    store = ClarificationStore(fake, _cfg())

    # The answer path stub always returns another ClarificationPrompt,
    # simulating persistent ambiguity after the user's reply.
    answer_path = _StillAmbiguousAnswerPath()
    processor = ClarificationReplyProcessor(store, answer_path)

    # Process the reply — this is the one permitted clarification being resolved.
    outcome = processor.process(clarification_id=clarification_id, reply=reply)

    # R2.7: The answer path was invoked exactly once (at most one clarification
    # attempt per original question).
    assert answer_path.call_count == 1

    # R2.8: Since the answer path returned another ClarificationPrompt (still
    # ambiguous), the processor MUST convert it to an AbstentionResponse.
    assert isinstance(outcome, AbstentionResponse), (
        f"Expected AbstentionResponse when still ambiguous, got {type(outcome).__name__}"
    )

    # R2.8: The outcome is never a further ClarificationPrompt.
    assert not isinstance(outcome, ClarificationPrompt)

    # R3.7 (inherited): The abstention contains no answer content.
    # AbstentionResponse has only reason_code, missing_information, trace_id.
    assert hasattr(outcome, "reason_code")
    assert hasattr(outcome, "missing_information")
    assert hasattr(outcome, "trace_id")
    # Verify no answer-like content leaks into the abstention.
    assert not hasattr(outcome, "answer") or getattr(outcome, "answer", None) is None
    assert not hasattr(outcome, "claims") or getattr(outcome, "claims", None) in (
        None,
        [],
    )
    assert not hasattr(outcome, "evidence_items") or getattr(
        outcome, "evidence_items", None
    ) in (None, [])

    # The missing_information field is bounded (1..1000 chars) and non-empty.
    assert 1 <= len(outcome.missing_information) <= 1000


# Feature: rag-trust-and-observability, Property 8: At most one clarification, then abstention
@settings(max_examples=200)
@given(
    original_question=_original_questions,
    reply=_replies,
    document_scope=_scopes,
    clarification_id=_clarification_ids,
)
def test_resolving_reply_yields_answer_not_abstention(
    original_question: str,
    reply: str,
    document_scope: list[str] | None,
    clarification_id: str,
) -> None:
    """When the reply resolves the ambiguity, the answer passes through directly.

    This confirms the system issues at most one clarification: if the single
    re-run of the answer path succeeds, the answer is returned directly (no
    further clarification is issued).

    **Validates: Requirements 2.7, 2.8**
    """
    fake = _FakeStore()
    _seed_record(
        fake,
        clarification_id=clarification_id,
        original_question=original_question,
        document_scope=document_scope,
    )
    store = ClarificationStore(fake, _cfg())

    # The answer path resolves successfully — no more ambiguity.
    answer_path = _ResolvingAnswerPath()
    processor = ClarificationReplyProcessor(store, answer_path)

    outcome = processor.process(clarification_id=clarification_id, reply=reply)

    # R2.7: The answer path is invoked exactly once.
    assert answer_path.call_count == 1

    # The resolved answer passes through directly.
    assert isinstance(outcome, UnifiedQueryResponse)
    assert outcome.answer == "The answer is 42."

    # No further clarification was issued.
    assert not isinstance(outcome, ClarificationPrompt)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
