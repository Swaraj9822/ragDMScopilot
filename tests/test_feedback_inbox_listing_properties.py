# Feature: rag-trust-and-observability, Property 19: Feedback inbox returns exactly the negative-rating items, paginated and filtered
"""Property-based test for ``list_feedback_inbox`` (R6).

Feature: rag-trust-and-observability (task 11.2).

**Property 19: Feedback inbox returns exactly the negative-rating items,
paginated and filtered.**

**Validates: Requirements 6.1, 6.4.**

Across arbitrary sets of feedback records (varied ratings 1-5, submission times,
feedback ids, and review statuses), a full walk of the cursor-paginated inbox
must:

* surface **only** Negative_Rating items (rating 1 or 2) — no rating 3-5 item
  ever appears (R6.1);
* honour the optional ``review_status`` filter — every surfaced item matches the
  requested status when one is supplied, and resolved items remain visible
  (R6.4, R6.8);
* order items **reverse-chronologically** by ``(created_at, feedback_id)`` across
  page boundaries (R6.1); and
* **partition** the qualifying items exactly once — every expected item appears,
  with no duplicates and no omissions, regardless of page size — matching an
  independent oracle and a single unpaginated listing.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from rag_system.feedback import (
    FeedbackListParams,
    list_feedback_inbox,
)
from rag_system.models import (
    FeedbackReviewRecord,
    QueryTraceRecord,
    ReviewStatus,
)

_SIGNING_KEY = "prop-19-pagination-secret"

# A small pool of timestamps so ties on ``created_at`` occur and the
# feedback-id tie-break is genuinely exercised.
_TIMESTAMPS = [
    "2024-01-01T00:00:00Z",
    "2024-01-02T00:00:00Z",
    "2024-01-03T00:00:00Z",
    "2024-01-04T00:00:00Z",
]


@st.composite
def _feedback_sets(draw: st.DrawFn) -> list[FeedbackReviewRecord]:
    """Generate a list of feedback records with unique feedback ids.

    Ratings span the full 1-5 scale (so both negative and non-negative items are
    present), timestamps are drawn from a small pool (to force ties), and review
    statuses cover all three states.
    """
    count = draw(st.integers(min_value=0, max_value=12))
    records: list[FeedbackReviewRecord] = []
    for index in range(count):
        rating = draw(st.integers(min_value=1, max_value=5))
        created_at = draw(st.sampled_from(_TIMESTAMPS))
        review_status = draw(st.sampled_from(list(ReviewStatus)))
        records.append(
            FeedbackReviewRecord(
                rating=rating,
                comment=draw(st.one_of(st.none(), st.text(max_size=8))),
                expected_answer=draw(st.one_of(st.none(), st.text(max_size=8))),
                trace_id=f"trace-{index}",
                feedback_id=f"f{index:02d}",
                created_at=created_at,
                review_status=review_status,
            )
        )
    return records


def _no_trace(_item: FeedbackReviewRecord) -> QueryTraceRecord | None:
    """Resolver used for listing/pagination properties (context join is R6.2)."""
    return None


def _expected_ids(
    records: list[FeedbackReviewRecord],
    review_status: ReviewStatus | None,
) -> list[str]:
    """Independent oracle: negative-rating + filter, reverse-chronological."""
    selected = [
        r
        for r in records
        if r.rating in (1, 2)
        and (review_status is None or r.review_status == review_status)
    ]
    selected.sort(key=lambda r: (r.created_at, r.feedback_id), reverse=True)
    return [r.feedback_id for r in selected]


def _walk_all_pages(
    records: list[FeedbackReviewRecord],
    *,
    review_status: ReviewStatus | None,
    page_size: int,
) -> list[str]:
    """Collect ids by walking every cursor-paginated page to exhaustion."""
    seen: list[str] = []
    cursor: str | None = None
    guard = 0
    while True:
        page = list_feedback_inbox(
            records,
            trace_of=_no_trace,
            params=FeedbackListParams(
                review_status=review_status,
                page_size=page_size,
                cursor=cursor,
            ),
            pagination_signing_key=_SIGNING_KEY,
            page_size_limit=page_size,
        )
        # No page may exceed the requested size.
        assert len(page.items) <= page_size
        seen.extend(c.feedback.feedback_id for c in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        guard += 1
        assert guard < 100, "pagination did not terminate"
    return seen


@given(
    records=_feedback_sets(),
    review_status=st.one_of(st.none(), st.sampled_from(list(ReviewStatus))),
    page_size=st.integers(min_value=1, max_value=6),
)
def test_feedback_inbox_negative_only_paginated_and_filtered(
    records: list[FeedbackReviewRecord],
    review_status: ReviewStatus | None,
    page_size: int,
) -> None:
    expected_ids = _expected_ids(records, review_status)

    walked = _walk_all_pages(
        records, review_status=review_status, page_size=page_size
    )

    # 1. Only negative-rating items appear, and each matches the filter (R6.1, R6.4).
    by_id = {r.feedback_id: r for r in records}
    for fid in walked:
        assert by_id[fid].rating in (1, 2)
        if review_status is not None:
            assert by_id[fid].review_status == review_status

    # 2. Reverse-chronological ordering holds across page boundaries (R6.1).
    keys = [(by_id[fid].created_at, fid) for fid in walked]
    assert keys == sorted(keys, reverse=True)

    # 3. Pagination partitions the qualifying items exactly once: every expected
    #    item appears, with no duplicates and no omissions (R6.1).
    assert walked == expected_ids
    assert len(walked) == len(set(walked))

    # 4. A single large page yields the identical result as the paginated walk.
    single = list_feedback_inbox(
        records,
        trace_of=_no_trace,
        params=FeedbackListParams(review_status=review_status, page_size=1000),
        pagination_signing_key=_SIGNING_KEY,
        page_size_limit=1000,
    )
    assert [c.feedback.feedback_id for c in single.items] == expected_ids
    assert single.next_cursor is None
