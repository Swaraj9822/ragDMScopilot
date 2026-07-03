# Feature: rag-trust-and-observability, Property 20: Feedback context is complete with empty values for absent fields
"""Property-based test for feedback context assembly (R6).

Feature: rag-trust-and-observability (task 11.3).

**Property 20: Feedback context is complete with empty values for absent
fields.**

**Validates: Requirements 6.2, 6.3.**

For every negative-rating item surfaced by ``list_feedback_inbox`` (and for the
underlying ``build_feedback_context`` join), the assembled ``FeedbackContext``
must:

* be **populated from the joined ``QueryTraceRecord``** — ``confidence``,
  ``route``, ``retrieved_chunks``, and ``sql`` are read from the trace, and the
  ``expected_answer`` from the feedback record (R6.2); and
* fall back to an **empty value for each absent field** — an absent SQL,
  comment, or expected answer collapses to an empty value, and when the joined
  trace is **missing or expired** the item is *still returned* with every
  context field empty (``confidence`` / ``route`` / ``sql`` / ``expected_answer``
  ``None`` and ``retrieved_chunks`` an empty list) rather than being dropped
  (R6.3, R6 missing-trace handling).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from rag_system.feedback import (
    FeedbackListParams,
    build_feedback_context,
    list_feedback_inbox,
)
from rag_system.models import (
    FeedbackReviewRecord,
    QueryTraceHit,
    QueryTraceRecord,
    ReviewStatus,
)

_SIGNING_KEY = "prop-20-pagination-secret"

_TIMESTAMPS = [
    "2024-01-01T00:00:00Z",
    "2024-01-02T00:00:00Z",
    "2024-01-03T00:00:00Z",
]


@st.composite
def _hits(draw: st.DrawFn) -> list[QueryTraceHit]:
    """Generate a small list of retrieved-hit records."""
    count = draw(st.integers(min_value=0, max_value=3))
    return [
        QueryTraceHit(
            chunk_id=f"c{i}",
            document_id=f"d{i}",
            version="v1",
            score=draw(st.floats(min_value=0.0, max_value=1.0)),
            source=draw(st.text(max_size=6)),
            text=draw(st.text(max_size=10)),
        )
        for i in range(count)
    ]


@st.composite
def _trace(draw: st.DrawFn, trace_id: str) -> QueryTraceRecord:
    """Generate a trace with possibly-absent SQL / confidence."""
    return QueryTraceRecord(
        trace_id=trace_id,
        question=draw(st.text(max_size=8)),
        route=draw(st.sampled_from(["documents", "database", "hybrid"])),
        answer=draw(st.text(max_size=8)),
        evidence_status=draw(st.sampled_from(["supported", "unsupported"])),
        confidence=draw(st.one_of(st.none(), st.sampled_from(["high", "low"]))),
        retrieved_hits=draw(_hits()),
        sql=draw(st.one_of(st.none(), st.text(min_size=1, max_size=12))),
    )


@st.composite
def _feedback_and_traces(
    draw: st.DrawFn,
) -> tuple[list[FeedbackReviewRecord], dict[str, QueryTraceRecord | None]]:
    """Generate negative-rating feedback records plus a per-item trace map.

    Each record's trace is independently present or absent (``None`` models a
    missing/expired trace). SQL, comment, and expected answer are each
    independently present or absent so the empty-value behaviour is exercised.
    """
    count = draw(st.integers(min_value=1, max_value=10))
    records: list[FeedbackReviewRecord] = []
    trace_map: dict[str, QueryTraceRecord | None] = {}
    for index in range(count):
        feedback_id = f"f{index:02d}"
        trace_id = f"trace-{index}"
        records.append(
            FeedbackReviewRecord(
                # Negative rating so the item is always surfaced by the inbox.
                rating=draw(st.integers(min_value=1, max_value=2)),
                comment=draw(st.one_of(st.none(), st.text(min_size=1, max_size=8))),
                expected_answer=draw(
                    st.one_of(st.none(), st.text(min_size=1, max_size=8))
                ),
                trace_id=trace_id,
                feedback_id=feedback_id,
                created_at=draw(st.sampled_from(_TIMESTAMPS)),
                review_status=draw(st.sampled_from(list(ReviewStatus))),
            )
        )
        present = draw(st.booleans())
        trace_map[feedback_id] = draw(_trace(trace_id)) if present else None
    return records, trace_map


def _assert_context_complete(
    item: FeedbackReviewRecord,
    trace: QueryTraceRecord | None,
    context,
) -> None:
    """Assert the context matches R6.2 population / R6.3 empty-value rules."""
    # The feedback record itself (rating, comment, review status) is always
    # carried, so absent comment surfaces as ``None`` on the nested record (R6.3).
    assert context.feedback == item
    assert context.feedback.comment == item.comment

    if trace is None:
        # Missing / expired trace: item still returned, every context field empty.
        assert context.expected_answer is None
        assert context.confidence is None
        assert context.route is None
        assert context.sql is None
        assert context.retrieved_chunks == []
        return

    # Trace present: context is populated from the joined trace (R6.2), with
    # absent expected answer / SQL collapsing to ``None`` (R6.3).
    assert context.expected_answer == item.expected_answer
    assert context.confidence == trace.confidence
    assert context.route == trace.route
    assert context.sql == trace.sql
    assert context.retrieved_chunks == list(trace.retrieved_hits)
    # Absent fields are empty, not fabricated.
    if trace.sql is None:
        assert context.sql is None
    if item.expected_answer is None:
        assert context.expected_answer is None


@given(data=_feedback_and_traces())
def test_build_feedback_context_is_complete_with_empty_absent_fields(
    data: tuple[list[FeedbackReviewRecord], dict[str, QueryTraceRecord | None]],
) -> None:
    """``build_feedback_context`` populates from the trace or uses empty values."""
    records, trace_map = data
    for item in records:
        trace = trace_map[item.feedback_id]
        context = build_feedback_context(item, trace)
        _assert_context_complete(item, trace, context)


@given(
    data=_feedback_and_traces(),
    page_size=st.integers(min_value=1, max_value=6),
)
def test_inbox_returns_every_item_with_complete_context(
    data: tuple[list[FeedbackReviewRecord], dict[str, QueryTraceRecord | None]],
    page_size: int,
) -> None:
    """Walking the inbox returns every negative item with complete context.

    No item is dropped when its trace is missing/expired, and each surfaced
    item's context obeys the population / empty-value rules (R6.2, R6.3).
    """
    records, trace_map = data

    def trace_of(item: FeedbackReviewRecord) -> QueryTraceRecord | None:
        return trace_map[item.feedback_id]

    seen: dict[str, object] = {}
    cursor: str | None = None
    guard = 0
    while True:
        page = list_feedback_inbox(
            records,
            trace_of=trace_of,
            params=FeedbackListParams(page_size=page_size, cursor=cursor),
            pagination_signing_key=_SIGNING_KEY,
            page_size_limit=page_size,
        )
        for context in page.items:
            fid = context.feedback.feedback_id
            assert fid not in seen, "item surfaced twice"
            seen[fid] = context
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        guard += 1
        assert guard < 100, "pagination did not terminate"

    # Every negative-rating item (all of them, by construction) is returned even
    # when its joined trace is missing/expired (R6 missing-trace handling).
    assert set(seen) == {r.feedback_id for r in records}

    by_id = {r.feedback_id: r for r in records}
    for fid, context in seen.items():
        item = by_id[fid]
        _assert_context_complete(item, trace_map[fid], context)
