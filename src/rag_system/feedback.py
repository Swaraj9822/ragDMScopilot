"""Feedback review inbox service (R6).

This module implements the pure logic behind the operator-only ``GET /feedback``
endpoint: a cursor-paginated listing of **negative-rating** feedback, ordered in
reverse-chronological order by submission time, each joined with its
:class:`~rag_system.models.QueryTraceRecord` to produce full context.

Design highlights (see design R6):

* **Negative-rating only (R6.1).** Only :class:`~rag_system.models.FeedbackReviewRecord`
  items with a :data:`NEGATIVE_RATINGS` rating (1 or 2 on the 1–5 scale) appear
  in the inbox. When none qualify the page is empty.
* **Reverse-chronological order (R6.1).** Items are ordered newest-first by
  ``created_at`` (submission time), tie-broken by ``feedback_id`` so the ordering
  is total and deterministic across pages.
* **Review-status filter (R6.4).** An optional ``review_status`` filter narrows
  the inbox to ``unreviewed`` / ``reviewed`` / ``resolved`` items; resolved items
  remain visible (R6.8).
* **Full context by trace join (R6.2).** Each item is joined with its
  ``QueryTraceRecord`` to read confidence, route, retrieved chunks, and SQL;
  the expected answer is carried on the feedback record itself.
* **Empty values for absent fields (R6.3) and missing-trace handling (R6).** A
  feedback item with no associated SQL, comment, or expected answer returns an
  empty value for each absent field. When the joined ``QueryTraceRecord`` is
  absent or has expired out of the trace store, the item is **still returned**
  with empty context fields (``expected_answer`` / ``confidence`` / ``route`` /
  ``sql`` empty and ``retrieved_chunks`` an empty list) rather than being dropped;
  the rating, comment, and ``review_status`` carried on the feedback record are
  always returned.
* **Signed, opaque cursors.** A cursor is a base64url token carrying the
  ``review_status`` context and the ``(created_at, feedback_id)`` boundary of the
  last item on the previous page, signed with HMAC-SHA256 keyed by
  ``pagination_signing_key`` (shared with the corpus listing). The signature is
  verified on decode and any tampered, truncated, or otherwise invalid token is
  rejected with ``invalid_cursor`` (never trusted or silently reset).

Like :mod:`rag_system.corpus`, this module operates on an in-memory sequence of
records plus a trace-resolver callable (the service reads the feedback records
and supplies the trace lookup), keeping this a pure, deterministic function that
is trivially unit- and property-testable without any storage or HTTP dependency.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256

from .models import (
    BenchmarkCase,
    FailureCategory,
    FeedbackContext,
    FeedbackInboxPage,
    FeedbackReviewRecord,
    QueryTraceRecord,
    ReviewStatus,
)

__all__ = [
    "NEGATIVE_RATINGS",
    "FeedbackListParams",
    "FeedbackError",
    "InvalidCursorError",
    "InvalidFailureCategoryError",
    "ExpectedAnswerRequiredError",
    "AlreadyInEvaluationSetError",
    "is_negative_rating",
    "list_feedback_inbox",
    "build_feedback_context",
    "encode_cursor",
    "decode_cursor",
    "parse_failure_category",
    "classify_feedback_record",
    "resolve_feedback_record",
    "benchmark_case_id_for",
    "build_benchmark_case",
    "promote_feedback_record",
]

#: The ratings considered a Negative_Rating (1 or 2 on the 1–5 scale) (R6.1).
NEGATIVE_RATINGS = frozenset({1, 2})

#: Resolves the joined trace for a feedback item, or ``None`` when the trace is
#: absent or has expired out of the trace store (R6 missing-trace handling).
TraceResolver = Callable[[FeedbackReviewRecord], QueryTraceRecord | None]


class FeedbackError(Exception):
    """Base class for feedback inbox errors carrying a stable ``code``.

    The ``code`` is the machine-readable error string the endpoint maps to a
    structured HTTP error body.
    """

    code = "feedback_error"


class InvalidCursorError(FeedbackError):
    """Raised when a pagination cursor is malformed, forged, or truncated."""

    code = "invalid_cursor"


class InvalidFailureCategoryError(FeedbackError):
    """Raised when a classification value is not one of the six Failure_Category
    values (R6.10). The stored category is left unchanged."""

    code = "invalid_failure_category"


class ExpectedAnswerRequiredError(FeedbackError):
    """Raised when promoting a Feedback_Item that has no expected answer (R6.7).
    No Benchmark_Case is created."""

    code = "expected_answer_required"


class AlreadyInEvaluationSetError(FeedbackError):
    """Raised when promoting a Feedback_Item already present in the
    Evaluation_Set (R6.11). No duplicate Benchmark_Case is created."""

    code = "already_in_evaluation_set"


@dataclass(frozen=True)
class FeedbackListParams:
    """Parameters for a single feedback inbox listing request.

    All fields are optional; the defaults produce a reverse-chronological listing
    of every negative-rating item at the maximum page size.
    """

    #: Filter by review status (R6.4); ``None`` lists all statuses.
    review_status: ReviewStatus | None = None
    #: Requested page size; clamped to the configured maximum.
    page_size: int | None = None
    #: Opaque, signed cursor from a previous page.
    cursor: str | None = None


def is_negative_rating(rating: int) -> bool:
    """Return whether ``rating`` is a Negative_Rating (1 or 2) (R6.1)."""
    return rating in NEGATIVE_RATINGS


# ---------------------------------------------------------------------------
# Cursor encoding / decoding (HMAC-signed, opaque)
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(token: str) -> bytes:
    # Restore the stripped ``=`` padding before decoding.
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + padding)
    except (binascii.Error, ValueError) as exc:  # malformed base64
        raise InvalidCursorError("Cursor is not valid base64.") from exc


def _sign(payload: bytes, signing_key: str | None) -> bytes:
    key = (signing_key or "").encode("utf-8")
    return hmac.new(key, payload, sha256).digest()


def _status_token(review_status: ReviewStatus | None) -> str:
    """Serialize the review-status filter for the cursor payload."""
    return "" if review_status is None else str(review_status)


def encode_cursor(
    *,
    review_status: ReviewStatus | None,
    created_at: str,
    last_id: str,
    signing_key: str | None,
) -> str:
    """Encode a signed, opaque cursor identifying the end of the current page.

    The payload records the ``review_status`` filter (so a cursor cannot be
    replayed against a differently-filtered listing) and the
    ``(created_at, feedback_id)`` boundary the next page resumes strictly after.
    """
    payload_obj = {
        "rs": _status_token(review_status),
        "ts": created_at,
        "id": last_id,
    }
    payload = json.dumps(payload_obj, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = _sign(payload, signing_key)
    return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"


@dataclass(frozen=True)
class _CursorBoundary:
    review_status: str
    created_at: str
    last_id: str


def decode_cursor(token: str, signing_key: str | None) -> _CursorBoundary:
    """Verify and decode a cursor, or raise :class:`InvalidCursorError`.

    Any deviation — wrong shape, bad base64, tampered payload, or a signature
    mismatch — is rejected rather than trusted or silently reset.
    """
    if not token:
        raise InvalidCursorError("Cursor is empty.")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidCursorError("Cursor is malformed.")

    payload_b64, signature_b64 = parts
    payload = _b64url_decode(payload_b64)
    signature = _b64url_decode(signature_b64)

    expected = _sign(payload, signing_key)
    # Constant-time comparison so a forged cursor cannot be probed byte by byte.
    if not hmac.compare_digest(signature, expected):
        raise InvalidCursorError("Cursor signature does not verify.")

    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("Cursor payload is not valid JSON.") from exc

    if not isinstance(obj, dict):
        raise InvalidCursorError("Cursor payload is not an object.")

    try:
        boundary = _CursorBoundary(
            review_status=str(obj["rs"]),
            created_at=str(obj["ts"]),
            last_id=str(obj["id"]),
        )
    except KeyError as exc:
        raise InvalidCursorError("Cursor payload is missing fields.") from exc

    # Reject a review-status value outside the supported vocabulary (the empty
    # string denotes "no filter").
    valid_statuses = {s.value for s in ReviewStatus} | {""}
    if boundary.review_status not in valid_statuses:
        raise InvalidCursorError("Cursor has an unknown review status.")

    return boundary


# ---------------------------------------------------------------------------
# Filtering, ordering, context assembly
# ---------------------------------------------------------------------------


def _select_negative(
    feedback_items: Iterable[FeedbackReviewRecord],
    review_status: ReviewStatus | None,
) -> list[FeedbackReviewRecord]:
    """Keep only negative-rating items matching the optional status filter."""
    selected: list[FeedbackReviewRecord] = []
    for item in feedback_items:
        if not is_negative_rating(item.rating):
            continue
        if review_status is not None and item.review_status != review_status:
            continue
        selected.append(item)
    return selected


def _ordering_key(item: FeedbackReviewRecord) -> tuple[str, str]:
    """Total-order key: submission time, tie-broken by the stable feedback id.

    The inbox is newest-first, so callers sort by this key in descending order.
    """
    return (item.created_at, item.feedback_id)


def build_feedback_context(
    item: FeedbackReviewRecord,
    trace: QueryTraceRecord | None,
) -> FeedbackContext:
    """Assemble a :class:`FeedbackContext` for one feedback item (R6.2, R6.3).

    Full context (confidence, route, retrieved chunks, SQL, and the expected
    answer) is read from the joined ``QueryTraceRecord``. When the trace is
    absent or expired (``trace is None``), the item is still returned with empty
    context fields (R6 missing-trace handling); the rating, comment, expected
    answer, and review status carried on the feedback record itself remain
    available via the nested ``feedback`` record.
    """
    if trace is None:
        # Missing/expired trace: return the item with empty context fields
        # rather than dropping it. Defaults leave every context field empty.
        return FeedbackContext(feedback=item)

    return FeedbackContext(
        feedback=item,
        # Absent expected answer / SQL collapse to ``None`` (empty) per R6.3.
        expected_answer=item.expected_answer,
        confidence=trace.confidence,
        route=trace.route,
        retrieved_chunks=list(trace.retrieved_hits),
        sql=trace.sql,
    )


def _clamp_page_size(requested: int | None, maximum: int) -> int:
    """Clamp the requested page size into ``[1, maximum]``."""
    limit = max(1, maximum)
    if requested is None:
        return limit
    return max(1, min(requested, limit))


def list_feedback_inbox(
    feedback_items: Sequence[FeedbackReviewRecord],
    *,
    trace_of: TraceResolver,
    params: FeedbackListParams,
    pagination_signing_key: str | None,
    page_size_limit: int,
) -> FeedbackInboxPage:
    """Produce one cursor-paginated :class:`FeedbackInboxPage` of the inbox.

    Pipeline (order matters): select negative-rating items → apply the
    ``review_status`` filter → order newest-first → resume at the cursor → slice
    to the clamped page size → join each with its trace → emit the next cursor.

    Args:
        feedback_items: The feedback records to draw the inbox from.
        trace_of: Resolves the joined ``QueryTraceRecord`` for an item, or
            ``None`` when absent/expired (R6 missing-trace handling).
        params: Review-status filter, page size, and cursor parameters.
        pagination_signing_key: HMAC-SHA256 key for cursor signing/verification.
        page_size_limit: The configured maximum page size.

    Returns:
        A :class:`FeedbackInboxPage` with the page of :class:`FeedbackContext`
        items and a ``next_cursor`` that is ``None`` on the final page. An empty
        page is returned when no negative-rating item matches (R6.1).

    Raises:
        InvalidCursorError: The cursor is malformed, forged, or truncated.
    """
    # 1. Negative-rating selection (R6.1) + review-status filter (R6.4).
    selected = _select_negative(feedback_items, params.review_status)

    # 2. Reverse-chronological total ordering (R6.1), consistent across pages.
    ordered = sorted(selected, key=_ordering_key, reverse=True)

    # 3. Resume strictly after the cursor boundary.
    if params.cursor is not None:
        boundary = decode_cursor(params.cursor, pagination_signing_key)
        # A cursor is only valid for the filter it was minted under; a mismatch
        # does not identify a valid position in this listing.
        if boundary.review_status != _status_token(params.review_status):
            raise InvalidCursorError("Cursor does not match the requested filter.")

        boundary_key = (boundary.created_at, boundary.last_id)
        # Newest-first: the next page holds items ordered strictly *before* the
        # boundary, i.e. with a smaller (created_at, id) key.
        ordered = [item for item in ordered if _ordering_key(item) < boundary_key]

    # 4. Clamp the page size and slice.
    page_size = _clamp_page_size(params.page_size, page_size_limit)
    page_items = ordered[:page_size]

    # 5. Join each item with its trace to assemble full context (R6.2, R6.3).
    contexts = [build_feedback_context(item, trace_of(item)) for item in page_items]

    # 6. Emit a next cursor only when further items remain (R6.1).
    next_cursor: str | None = None
    if len(ordered) > page_size and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(
            review_status=params.review_status,
            created_at=last.created_at,
            last_id=last.feedback_id,
            signing_key=pagination_signing_key,
        )

    return FeedbackInboxPage(items=contexts, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Review actions: classify (R6.5/R6.10), resolve (R6.8), promote (R6.6/R6.7/R6.11)
#
# These are pure transformations over a :class:`FeedbackReviewRecord`: each
# returns a new record (and, for promotion, a derived :class:`BenchmarkCase`)
# without touching storage, so the state transitions are trivially unit- and
# property-testable. The service layer wraps them with ETag-CAS writes.
# ---------------------------------------------------------------------------


def parse_failure_category(value: str | FailureCategory) -> FailureCategory:
    """Validate a submitted classification against the six Failure_Category values.

    Accepts either a :class:`FailureCategory` (returned as-is) or its string
    value (e.g. ``"Missing knowledge"``). Any other value is rejected with
    :class:`InvalidFailureCategoryError` (R6.10) so the caller never persists an
    out-of-vocabulary category.
    """
    if isinstance(value, FailureCategory):
        return value
    try:
        return FailureCategory(value)
    except ValueError as exc:
        raise InvalidFailureCategoryError(
            f"{value!r} is not a valid Failure_Category."
        ) from exc


def classify_feedback_record(
    record: FeedbackReviewRecord,
    *,
    category: str | FailureCategory,
    reviewer: str,
    reviewed_at: str,
) -> FeedbackReviewRecord:
    """Classify a Feedback_Item with a Failure_Category (R6.5, R6.10).

    Validates ``category`` against the six allowed values (raising
    :class:`InvalidFailureCategoryError` otherwise, leaving the record
    unchanged), then returns a copy carrying the assigned category, the
    classifying operator's identity, the review timestamp, and
    ``review_status == reviewed`` — **replacing** any previously assigned
    Failure_Category.
    """
    parsed = parse_failure_category(category)
    return record.model_copy(
        update={
            "failure_category": parsed,
            "reviewed_by": reviewer,
            "reviewed_at": reviewed_at,
            "review_status": ReviewStatus.reviewed,
        }
    )


def resolve_feedback_record(record: FeedbackReviewRecord) -> FeedbackReviewRecord:
    """Mark a Feedback_Item as resolved (R6.8).

    Sets ``review_status == resolved``; the item stays in the inbox and remains
    filterable by review status (the listing never drops resolved items).
    """
    return record.model_copy(update={"review_status": ReviewStatus.resolved})


def benchmark_case_id_for(feedback_id: str) -> str:
    """Deterministic Benchmark_Case id derived from a Feedback_Item.

    Deriving the id from ``feedback_id`` (rather than a random id) makes
    promotion idempotent at the storage layer: a second promotion targets the
    same immutable key, so a create-only write cannot silently produce a
    duplicate (R6.11).
    """
    return f"feedback-{feedback_id}"


def build_benchmark_case(
    record: FeedbackReviewRecord,
    *,
    question: str,
) -> BenchmarkCase:
    """Build a Benchmark_Case from a Feedback_Item's question + expected answer.

    The caller is responsible for the R6.7 (expected answer present) and R6.11
    (not already promoted) guards; :func:`promote_feedback_record` enforces both.
    The case is marked ``human_reviewed`` because it originates from an operator
    review of real negative feedback.
    """
    return BenchmarkCase(
        id=benchmark_case_id_for(record.feedback_id),
        question=question,
        expected_answer=record.expected_answer,
        human_reviewed=True,
    )


def promote_feedback_record(
    record: FeedbackReviewRecord,
    *,
    question: str,
) -> tuple[FeedbackReviewRecord, BenchmarkCase]:
    """Promote a reviewed Feedback_Item into the Evaluation_Set (R6.6/R6.7/R6.11).

    Guards (in precedence order):

    * **Already promoted (R6.11).** When ``promoted_case_id`` is already set,
      raise :class:`AlreadyInEvaluationSetError` — no duplicate case is created.
    * **No expected answer (R6.7).** When the item has no (non-empty) expected
      answer, raise :class:`ExpectedAnswerRequiredError` — no case is created.

    On success returns ``(updated_record, benchmark_case)`` where the record
    carries ``promoted_case_id`` (the de-dup guard for future promotions) and the
    :class:`BenchmarkCase` is derived from the question + expected answer (R6.6).
    """
    if record.promoted_case_id is not None:
        raise AlreadyInEvaluationSetError(
            "Feedback_Item is already present in the Evaluation_Set."
        )
    if not (record.expected_answer or "").strip():
        raise ExpectedAnswerRequiredError(
            "An expected answer is required to promote the Feedback_Item."
        )

    case = build_benchmark_case(record, question=question)
    updated = record.model_copy(update={"promoted_case_id": case.id})
    return updated, case
