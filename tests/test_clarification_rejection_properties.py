"""Property-based tests for invalid/expired/empty clarification reply rejection (R2.5, R2.6).

Feature: rag-trust-and-observability (task 4.5).

This module hosts the numbered correctness property for the rejection of invalid,
expired, or empty clarification replies. It drives ``ClarificationReplyProcessor.process``
with random invalid/expired clarification ids and empty/whitespace-only replies,
asserting that each case is rejected with the appropriate error code and that the
answer path is never invoked for an invalid request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.clarification import (
    ClarificationInvalidOrExpiredError,
    ClarificationReplyProcessor,
    ClarificationReplyRequiredError,
    ClarificationStore,
)
from rag_system.models import (
    ClarificationRecord,
    UnifiedQueryResponse,
)
from rag_system.storage import clarification_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStore:
    """Create-only JSON store double that supports both writes and reads."""

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


class _RecordingAnswerPath:
    """Answer path double that records calls and returns a configured value."""

    def __init__(self, result: object | None = None) -> None:
        self.result = result or _answer()
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


def _store_valid_record(
    fake: _FakeStore,
    *,
    clarification_id: str,
    expiry: datetime | None = None,
) -> ClarificationRecord:
    """Persist a valid, unexpired ClarificationRecord into the fake store."""
    if expiry is None:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    record = ClarificationRecord(
        clarification_id=clarification_id,
        conversation_turn_id="conv-1:0",
        original_question="What was revenue?",
        document_scope=["doc-a"],
        clarification_expiry=expiry.isoformat(),
    )
    fake.writes[clarification_key(clarification_id)] = record.model_dump()
    return record


def _store_expired_record(
    fake: _FakeStore,
    *,
    clarification_id: str,
    expired_minutes_ago: int = 5,
) -> ClarificationRecord:
    """Persist an expired ClarificationRecord into the fake store."""
    expiry = datetime.now(timezone.utc) - timedelta(minutes=expired_minutes_ago)
    return _store_valid_record(fake, clarification_id=clarification_id, expiry=expiry)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Random strings that won't match any stored clarification_id.
_unknown_ids = st.text(min_size=1, max_size=100).filter(lambda s: s.strip() != "")

# Expired durations: how many minutes ago the record expired.
_expired_minutes = st.integers(min_value=1, max_value=10080)  # up to 1 week

# Empty or whitespace-only reply strings (R2.6).
_empty_replies = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r\x0b\x0c", min_size=0, max_size=50),
)

# Non-empty replies for tests where the reply content doesn't matter.
_valid_replies = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")


# ---------------------------------------------------------------------------
# Feature: rag-trust-and-observability, Property 7: Invalid or expired clarification replies are rejected
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(unknown_id=_unknown_ids, reply=_valid_replies)
def test_unknown_clarification_id_rejected(unknown_id: str, reply: str) -> None:
    """An unknown clarification_id is rejected with clarification_invalid_or_expired.

    **Validates: Requirements 2.5**

    For any reply referencing an unknown clarification_id (one with no stored
    record), the system rejects the reply with the clarification_invalid_or_expired
    error code and never invokes the answer path.
    """
    fake = _FakeStore()
    # No record stored — the id is unknown.
    store = ClarificationStore(fake, _settings())
    answer_path = _RecordingAnswerPath()
    processor = ClarificationReplyProcessor(store, answer_path)

    with pytest.raises(ClarificationInvalidOrExpiredError) as exc:
        processor.process(clarification_id=unknown_id, reply=reply)

    assert exc.value.code == "clarification_invalid_or_expired"
    # The answer path must never run for an invalid id.
    assert answer_path.calls == []


@settings(max_examples=200)
@given(expired_minutes_ago=_expired_minutes, reply=_valid_replies)
def test_expired_clarification_id_rejected(expired_minutes_ago: int, reply: str) -> None:
    """An expired clarification_id is rejected with clarification_invalid_or_expired.

    **Validates: Requirements 2.5**

    For any reply referencing a clarification_id whose Clarification_Expiry has
    passed, the system rejects the reply with the clarification_invalid_or_expired
    error code and never invokes the answer path.
    """
    fake = _FakeStore()
    cid = f"expired-cid-{expired_minutes_ago}"
    _store_expired_record(fake, clarification_id=cid, expired_minutes_ago=expired_minutes_ago)

    store = ClarificationStore(fake, _settings())
    answer_path = _RecordingAnswerPath()
    processor = ClarificationReplyProcessor(store, answer_path)

    with pytest.raises(ClarificationInvalidOrExpiredError) as exc:
        processor.process(clarification_id=cid, reply=reply)

    assert exc.value.code == "clarification_invalid_or_expired"
    # The answer path must never run for an expired id.
    assert answer_path.calls == []


@settings(max_examples=200)
@given(empty_reply=_empty_replies)
def test_empty_or_whitespace_reply_rejected(empty_reply: str) -> None:
    """An empty or whitespace-only reply is rejected with clarification_reply_required.

    **Validates: Requirements 2.6**

    For any reply string that is empty or consists only of whitespace characters,
    submitted against a valid (existing, unexpired) clarification_id, the system
    rejects the reply with the clarification_reply_required error code and never
    invokes the answer path.
    """
    fake = _FakeStore()
    cid = "valid-cid-001"
    _store_valid_record(fake, clarification_id=cid)

    store = ClarificationStore(fake, _settings())
    answer_path = _RecordingAnswerPath()
    processor = ClarificationReplyProcessor(store, answer_path)

    with pytest.raises(ClarificationReplyRequiredError) as exc:
        processor.process(clarification_id=cid, reply=empty_reply)

    assert exc.value.code == "clarification_reply_required"
    # The answer path must never run for an empty reply.
    assert answer_path.calls == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
