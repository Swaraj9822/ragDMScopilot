"""Property-based tests for ambiguity clarification issuance (R2).

Feature: rag-trust-and-observability (task 4.2).

This module hosts the numbered correctness property for clarification prompt
issuance. It drives ``ClarificationStore.issue`` with a fake create-only store
and asserts that every issued :class:`ClarificationPrompt` is well-formed and
that its ``clarification_id`` is unguessable and unique across issues.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.clarification import ClarificationStore
from rag_system.models import ClarificationRecord
from rag_system.storage import clarification_key


class _FakeCreateOnlyStore:
    """Create-only JSON store double.

    Mirrors the ``GcsArtifactStore.create_json`` contract the module depends on:
    a second write to an existing key is a hard error, so a persisted
    clarification record is immutable and its id is necessarily unique.
    """

    def __init__(self) -> None:
        self.writes: dict[str, object] = {}

    def create_json(self, key: str, payload: object) -> str:
        if key in self.writes:
            raise AssertionError(f"duplicate create for {key}")
        self.writes[key] = payload
        return f"s3://fake/{key}"


def _settings(expiry_minutes: int) -> SimpleNamespace:
    return SimpleNamespace(clarification_expiry_minutes=expiry_minutes)


# ``secrets.token_urlsafe`` emits base64url characters only.
_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# token_urlsafe(32) yields ~43 chars; require a high-entropy floor so the id
# cannot be guessed or enumerated.
_MIN_UNGUESSABLE_LEN = 32


# Non-whitespace clarification questions: a well-formed prompt carries exactly
# one non-empty question.
_questions = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")
_turn_ids = st.text(min_size=1, max_size=64).filter(lambda s: s.strip() != "")
_original_questions = st.text(min_size=1, max_size=200)
_scopes = st.one_of(
    st.none(),
    st.lists(st.text(min_size=1, max_size=32), max_size=6),
)


# Feature: rag-trust-and-observability, Property 5: Clarification prompts are well-formed and unguessable
@settings(max_examples=200)
@given(
    original_question=_original_questions,
    conversation_turn_id=_turn_ids,
    clarification_question=_questions,
    document_scope=_scopes,
    expiry_minutes=st.integers(min_value=1, max_value=1440),
)
def test_issued_clarification_prompts_are_well_formed_and_unguessable(
    _seen_ids: set,
    original_question: str,
    conversation_turn_id: str,
    clarification_question: str,
    document_scope: list[str] | None,
    expiry_minutes: int,
) -> None:
    """Every issued prompt is well-formed and carries an unguessable, unique id.

    Validates: Requirements 2.2 — a returned Clarification_Prompt includes
    exactly one clarification question, a unique clarification_id, the
    originating Conversation_Turn_ID, a Clarification_Expiry, and the bound
    document scope.
    """
    store = _FakeCreateOnlyStore()
    clarifications = ClarificationStore(store, _settings(expiry_minutes))

    issued_at = datetime.now(timezone.utc)
    prompt = clarifications.issue(
        original_question=original_question,
        conversation_turn_id=conversation_turn_id,
        clarification_question=clarification_question,
        document_scope=document_scope,
    )

    # --- Exactly one clarification question, non-empty, echoing the input. ----
    assert isinstance(prompt.clarification_question, str)
    assert prompt.clarification_question == clarification_question
    assert prompt.clarification_question.strip() != ""

    # --- Unguessable id: URL-safe, high-entropy floor. -----------------------
    cid = prompt.clarification_id
    assert isinstance(cid, str)
    assert len(cid) >= _MIN_UNGUESSABLE_LEN
    assert _URLSAFE_RE.match(cid) is not None

    # --- Unique across every issue in this run. ------------------------------
    assert cid not in _seen_ids
    _seen_ids.add(cid)

    # --- Conversation turn binding. ------------------------------------------
    assert prompt.conversation_turn_id == conversation_turn_id

    # --- Expiry present, tz-aware, and strictly in the future. ---------------
    expiry = datetime.fromisoformat(prompt.clarification_expiry)
    assert expiry.tzinfo is not None
    assert expiry > issued_at

    # --- Document scope binding on the prompt. -------------------------------
    assert prompt.document_scope == document_scope

    # --- The persisted record binds the same id -> turn/scope/question/expiry.
    key = clarification_key(cid)
    assert key in store.writes
    record = ClarificationRecord.model_validate(store.writes[key])
    assert record.clarification_id == cid
    assert record.conversation_turn_id == conversation_turn_id
    assert record.original_question == original_question
    assert record.document_scope == document_scope
    assert record.clarification_expiry == prompt.clarification_expiry


@pytest.fixture(scope="module")
def _seen_ids() -> set:
    """Shared registry of issued ids, asserting global uniqueness across the run."""
    return set()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
