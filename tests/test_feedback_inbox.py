"""Unit tests for the feedback review inbox service (R6).

Feature: rag-trust-and-observability (task 11.1).

Covers negative-rating selection and reverse-chronological ordering (R6.1),
full context assembly via the trace join (R6.2), empty values for absent
SQL/comment/expected-answer and the missing-trace path (R6.3, R6 missing-trace
handling), review-status filtering including resolved visibility (R6.4, R6.8),
and signed cursor pagination.
"""

from __future__ import annotations

import pytest

from rag_system.feedback import (
    FeedbackListParams,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
    is_negative_rating,
    list_feedback_inbox,
)
from rag_system.models import (
    FeedbackReviewRecord,
    QueryTraceHit,
    QueryTraceRecord,
    ReviewStatus,
)

_SIGNING_KEY = "test-pagination-secret"


def _feedback(
    feedback_id: str,
    *,
    rating: int = 1,
    created_at: str,
    trace_id: str | None = None,
    comment: str | None = "bad answer",
    expected_answer: str | None = "the expected answer",
    review_status: ReviewStatus = ReviewStatus.unreviewed,
) -> FeedbackReviewRecord:
    return FeedbackReviewRecord(
        rating=rating,
        comment=comment,
        expected_answer=expected_answer,
        trace_id=trace_id or f"trace-{feedback_id}",
        feedback_id=feedback_id,
        created_at=created_at,
        review_status=review_status,
    )


def _trace(
    trace_id: str,
    *,
    route: str = "documents",
    confidence: str | None = "high",
    sql: str | None = None,
    hits: list[QueryTraceHit] | None = None,
) -> QueryTraceRecord:
    return QueryTraceRecord(
        trace_id=trace_id,
        question="a question",
        route=route,
        answer="an answer",
        evidence_status="supported",
        confidence=confidence,
        sql=sql,
        retrieved_hits=hits or [],
    )


def _resolver(traces: dict[str, QueryTraceRecord]):
    def trace_of(item: FeedbackReviewRecord) -> QueryTraceRecord | None:
        return traces.get(item.trace_id)

    return trace_of


def _list(items, *, traces=None, params=None, page_size_limit=50):
    return list_feedback_inbox(
        items,
        trace_of=_resolver(traces or {}),
        params=params or FeedbackListParams(),
        pagination_signing_key=_SIGNING_KEY,
        page_size_limit=page_size_limit,
    )


# ---------------------------------------------------------------------------
# Negative-rating selection and ordering (R6.1)
# ---------------------------------------------------------------------------


def test_is_negative_rating():
    assert is_negative_rating(1) and is_negative_rating(2)
    assert not is_negative_rating(3)
    assert not is_negative_rating(4)
    assert not is_negative_rating(5)


def test_only_negative_ratings_are_listed():
    items = [
        _feedback("f1", rating=1, created_at="2024-01-01T00:00:00Z"),
        _feedback("f2", rating=3, created_at="2024-01-02T00:00:00Z"),
        _feedback("f3", rating=2, created_at="2024-01-03T00:00:00Z"),
        _feedback("f4", rating=5, created_at="2024-01-04T00:00:00Z"),
    ]
    page = _list(items)
    assert {c.feedback.feedback_id for c in page.items} == {"f1", "f3"}


def test_empty_collection_when_no_negative_ratings():
    items = [
        _feedback("f1", rating=3, created_at="2024-01-01T00:00:00Z"),
        _feedback("f2", rating=4, created_at="2024-01-02T00:00:00Z"),
    ]
    page = _list(items)
    assert page.items == []
    assert page.next_cursor is None


def test_reverse_chronological_order():
    items = [
        _feedback("f1", created_at="2024-01-01T00:00:00Z"),
        _feedback("f2", created_at="2024-01-03T00:00:00Z"),
        _feedback("f3", created_at="2024-01-02T00:00:00Z"),
    ]
    page = _list(items)
    order = [c.feedback.feedback_id for c in page.items]
    assert order == ["f2", "f3", "f1"]


def test_ties_broken_deterministically_by_feedback_id():
    ts = "2024-01-01T00:00:00Z"
    items = [
        _feedback("f-a", created_at=ts),
        _feedback("f-c", created_at=ts),
        _feedback("f-b", created_at=ts),
    ]
    page = _list(items)
    order = [c.feedback.feedback_id for c in page.items]
    # Same timestamp: newest-first on (created_at, id) descending => id desc.
    assert order == ["f-c", "f-b", "f-a"]


# ---------------------------------------------------------------------------
# Full context assembly (R6.2)
# ---------------------------------------------------------------------------


def test_context_joined_from_trace():
    hit = QueryTraceHit(
        chunk_id="c1",
        document_id="d1",
        version="v1",
        score=0.9,
        source="documents",
        text="supporting passage",
    )
    items = [_feedback("f1", created_at="2024-01-01T00:00:00Z", trace_id="t1")]
    traces = {
        "t1": _trace(
            "t1", route="sql", confidence="medium", sql="SELECT 1", hits=[hit]
        )
    }
    page = _list(items, traces=traces)
    ctx = page.items[0]
    assert ctx.route == "sql"
    assert ctx.confidence == "medium"
    assert ctx.sql == "SELECT 1"
    assert [h.chunk_id for h in ctx.retrieved_chunks] == ["c1"]
    assert ctx.expected_answer == "the expected answer"
    # Rating/comment/review status are always available on the nested record.
    assert ctx.feedback.rating == 1
    assert ctx.feedback.comment == "bad answer"
    assert ctx.feedback.review_status == ReviewStatus.unreviewed


# ---------------------------------------------------------------------------
# Empty values for absent fields + missing trace (R6.3, R6 missing-trace)
# ---------------------------------------------------------------------------


def test_absent_optional_fields_are_empty():
    items = [
        _feedback(
            "f1",
            created_at="2024-01-01T00:00:00Z",
            trace_id="t1",
            comment=None,
            expected_answer=None,
        )
    ]
    traces = {"t1": _trace("t1", sql=None)}
    page = _list(items, traces=traces)
    ctx = page.items[0]
    assert ctx.expected_answer is None
    assert ctx.sql is None
    assert ctx.feedback.comment is None


def test_missing_trace_returns_item_with_empty_context():
    items = [_feedback("f1", created_at="2024-01-01T00:00:00Z", trace_id="gone")]
    # No trace registered => resolver returns None (absent/expired).
    page = _list(items, traces={})
    assert len(page.items) == 1
    ctx = page.items[0]
    assert ctx.expected_answer is None
    assert ctx.confidence is None
    assert ctx.route is None
    assert ctx.sql is None
    assert ctx.retrieved_chunks == []
    # The feedback record itself is still fully returned.
    assert ctx.feedback.feedback_id == "f1"
    assert ctx.feedback.rating == 1
    assert ctx.feedback.review_status == ReviewStatus.unreviewed


# ---------------------------------------------------------------------------
# Review-status filtering (R6.4, R6.8)
# ---------------------------------------------------------------------------


def test_filter_by_review_status():
    items = [
        _feedback("f1", created_at="2024-01-01T00:00:00Z", review_status=ReviewStatus.unreviewed),
        _feedback("f2", created_at="2024-01-02T00:00:00Z", review_status=ReviewStatus.reviewed),
        _feedback("f3", created_at="2024-01-03T00:00:00Z", review_status=ReviewStatus.resolved),
    ]
    page = _list(items, params=FeedbackListParams(review_status=ReviewStatus.reviewed))
    assert {c.feedback.feedback_id for c in page.items} == {"f2"}


def test_resolved_items_remain_visible():
    items = [
        _feedback("f1", created_at="2024-01-01T00:00:00Z", review_status=ReviewStatus.resolved),
    ]
    # Unfiltered inbox still shows resolved items (R6.8).
    page_all = _list(items)
    assert {c.feedback.feedback_id for c in page_all.items} == {"f1"}
    # And they are filterable by the resolved status.
    page_resolved = _list(
        items, params=FeedbackListParams(review_status=ReviewStatus.resolved)
    )
    assert {c.feedback.feedback_id for c in page_resolved.items} == {"f1"}


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


def test_pagination_partitions_items_exactly_once():
    items = [
        _feedback(f"f{i}", created_at=f"2024-01-{i:02d}T00:00:00Z") for i in range(1, 8)
    ]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = _list(
            items,
            params=FeedbackListParams(page_size=3, cursor=cursor),
            page_size_limit=3,
        )
        seen.extend(c.feedback.feedback_id for c in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages < 10, "pagination did not terminate"

    # Every item exactly once, in reverse-chronological order, no duplicates.
    assert seen == [f"f{i}" for i in range(7, 0, -1)]
    assert len(seen) == len(set(seen)) == 7


def test_final_page_has_null_cursor():
    items = [
        _feedback(f"f{i}", created_at=f"2024-01-{i:02d}T00:00:00Z") for i in range(1, 4)
    ]
    page = _list(items, params=FeedbackListParams(page_size=10), page_size_limit=10)
    assert page.next_cursor is None
    assert len(page.items) == 3


def test_tampered_cursor_is_rejected():
    items = [
        _feedback(f"f{i}", created_at=f"2024-01-{i:02d}T00:00:00Z") for i in range(1, 6)
    ]
    page = _list(items, params=FeedbackListParams(page_size=2), page_size_limit=2)
    assert page.next_cursor is not None
    tampered = page.next_cursor[:-2] + ("aa" if not page.next_cursor.endswith("aa") else "bb")
    with pytest.raises(InvalidCursorError):
        _list(
            items,
            params=FeedbackListParams(page_size=2, cursor=tampered),
            page_size_limit=2,
        )


def test_cursor_bound_to_review_status_filter():
    items = [
        _feedback(f"f{i}", created_at=f"2024-01-{i:02d}T00:00:00Z", review_status=ReviewStatus.unreviewed)
        for i in range(1, 6)
    ]
    page = _list(
        items,
        params=FeedbackListParams(review_status=ReviewStatus.unreviewed, page_size=2),
        page_size_limit=2,
    )
    assert page.next_cursor is not None
    # Replaying the cursor under a different filter is rejected.
    with pytest.raises(InvalidCursorError):
        _list(
            items,
            params=FeedbackListParams(review_status=ReviewStatus.reviewed, cursor=page.next_cursor),
        )


def test_cursor_round_trips():
    token = encode_cursor(
        review_status=ReviewStatus.reviewed,
        created_at="2024-01-01T00:00:00Z",
        last_id="f1",
        signing_key=_SIGNING_KEY,
    )
    boundary = decode_cursor(token, _SIGNING_KEY)
    assert boundary.review_status == "reviewed"
    assert boundary.created_at == "2024-01-01T00:00:00Z"
    assert boundary.last_id == "f1"
