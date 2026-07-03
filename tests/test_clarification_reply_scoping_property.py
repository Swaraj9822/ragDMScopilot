# Feature: rag-trust-and-observability, Property 6: Clarification replies are scoped to their clarification record
"""Property-based test for clarification reply scoping (R2.4).

Validates that when a user submits a reply to a clarification, the answer path
is re-run with the combined question scoped to exactly the document_scope from
the ClarificationRecord — never a different scope or the full corpus.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.clarification import (
    ClarificationReplyProcessor,
    ClarificationStore,
    combine_question_and_reply,
)
from rag_system.models import (
    UnifiedQueryResponse,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Non-empty original questions.
_original_questions = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")

#: Non-empty, non-whitespace-only replies (valid replies per R2.6).
_replies = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")

#: Document scopes — always present (list of 1+ doc ids) since Property 6 is
#: about scoped replies. Also include None to verify the full-corpus fallback.
_document_scopes = st.one_of(
    st.none(),
    st.lists(st.text(min_size=1, max_size=40), min_size=1, max_size=8),
)

#: Conversation turn IDs.
_turn_ids = st.text(min_size=1, max_size=64).filter(lambda s: s.strip() != "")

#: Expiry offset in minutes — always positive so the record is unexpired.
_expiry_minutes = st.integers(min_value=5, max_value=1440)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory JSON store that records writes and supports reads."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def create_json(self, key: str, payload: object) -> str:
        self._data[key] = payload
        return f"fake://{key}"

    def get_json(self, key: str) -> object | None:
        return self._data.get(key)


class _CapturingAnswerPath:
    """Answer path stub that captures the (question, document_scope) it was called with.

    Returns a minimal valid UnifiedQueryResponse so the processor completes
    normally and we can inspect the arguments it passed.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, *, question: str, document_scope: list[str] | None
    ) -> UnifiedQueryResponse:
        self.calls.append({"question": question, "document_scope": document_scope})
        return UnifiedQueryResponse(
            answer="Stub answer.",
            route="rag",
            evidence_status="supported",
            trace_id="trace-stub",
        )


def _make_settings(expiry_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(clarification_expiry_minutes=expiry_minutes)


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 6: Clarification replies are scoped to their clarification record
@settings(max_examples=200)
@given(
    original_question=_original_questions,
    reply=_replies,
    conversation_turn_id=_turn_ids,
    document_scope=_document_scopes,
    expiry_minutes=_expiry_minutes,
)
def test_clarification_reply_is_scoped_to_record_document_scope(
    original_question: str,
    reply: str,
    conversation_turn_id: str,
    document_scope: list[str] | None,
    expiry_minutes: int,
) -> None:
    """The answer path is re-run with exactly the document_scope from the record.

    **Validates: Requirements 2.4**

    For any valid, unexpired clarification_id and any non-empty reply, the
    system processes the combined question scoped to the document_scope stored
    on the ClarificationRecord — not some other scope and not the full corpus
    unless the record itself has None scope.
    """
    # --- Arrange: issue a clarification so a valid record exists. -------------
    fake_store = _FakeStore()
    cfg = _make_settings(expiry_minutes)
    clarification_store = ClarificationStore(fake_store, cfg)

    prompt = clarification_store.issue(
        original_question=original_question,
        conversation_turn_id=conversation_turn_id,
        clarification_question="Please clarify.",
        document_scope=document_scope,
    )
    clarification_id = prompt.clarification_id

    # --- Arrange: capturing answer path. -------------------------------------
    answer_path = _CapturingAnswerPath()
    processor = ClarificationReplyProcessor(clarification_store, answer_path)

    # --- Act: submit the reply. -----------------------------------------------
    outcome = processor.process(clarification_id=clarification_id, reply=reply)

    # --- Assert: answer path was called exactly once. -------------------------
    assert len(answer_path.calls) == 1, "Answer path must be invoked exactly once per reply."

    call = answer_path.calls[0]

    # --- Assert: scope passed to the answer path matches the record's scope. --
    assert call["document_scope"] == document_scope, (
        f"Expected answer path to receive document_scope={document_scope!r} "
        f"from the ClarificationRecord, but got {call['document_scope']!r}."
    )

    # --- Assert: question is the combined original + reply. -------------------
    expected_question = combine_question_and_reply(original_question, reply)
    assert call["question"] == expected_question, (
        "The answer path must receive the original question combined with the reply."
    )

    # --- Assert: the outcome is a valid answer (not an abstention or prompt). -
    assert isinstance(outcome, UnifiedQueryResponse), (
        "When the answer path succeeds, the reply processor should return its response."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
