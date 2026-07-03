"""Ambiguity clarification support (R2).

When the router classifies a question as ambiguous, the system asks a single
focused clarifying question instead of guessing (R2.1). This module owns the
persistence side of that flow: it mints an unguessable ``clarification_id``,
binds it (create-only, so it is immutable once written) to the originating
conversation turn, the document scope, the original question, and an expiry
timestamp, and returns the :class:`ClarificationPrompt` the API surfaces to the
caller (R2.2).

Reply processing (validating an incoming ``clarification_id`` and re-running the
answer path) lives in :class:`ClarificationReplyProcessor` below: it validates
the referenced record's existence and expiry and the non-empty reply, re-runs
the answer path with the combined question scoped to the record's
``document_scope`` and the ambiguous branch disabled, and abstains if the
ambiguity is still unresolved (R2.4–R2.8).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol

from rag_system.config import Settings
from rag_system.models import (
    AbstentionResponse,
    ClarificationPrompt,
    ClarificationRecord,
    ReasonCode,
    UnifiedQueryResponse,
)
from rag_system.observability import get_logger, get_trace_id, metrics
from rag_system.storage import clarification_key

logger = get_logger(__name__)

#: Number of random bytes behind an issued ``clarification_id``. 32 bytes
#: (256 bits) via ``secrets.token_urlsafe`` makes the id unguessable and
#: collision-free for practical purposes (R2.2).
_CLARIFICATION_ID_BYTES = 32

#: Fixed wording for scope ambiguity (R2.9): the system cannot tell whether to
#: search the caller's selected documents or the entire corpus, so it asks.
SCOPE_CLARIFICATION_QUESTION = (
    "Should I search only the documents you've selected, or the entire corpus?"
)

#: Fallback wording when the classifier flags ambiguity but supplies no focused
#: question of its own.
DEFAULT_CLARIFICATION_QUESTION = (
    "Could you clarify what you're asking about so I can answer accurately?"
)


class ClarificationError(Exception):
    """Base class for clarification reply errors carrying a stable ``code``.

    The ``code`` is the machine-readable error string the ``/ask/clarify``
    endpoint (task 5.1) maps to a structured HTTP 400 error body.
    """

    code = "clarification_error"


class ClarificationInvalidOrExpiredError(ClarificationError):
    """Raised when a reply references an unknown or expired ``clarification_id`` (R2.5)."""

    code = "clarification_invalid_or_expired"


class ClarificationReplyRequiredError(ClarificationError):
    """Raised when a clarification reply is empty or whitespace-only (R2.6)."""

    code = "clarification_reply_required"


class JsonStore(Protocol):
    """The slice of :class:`~rag_system.storage.S3ArtifactStore` this module needs."""

    def create_json(self, key: str, payload: object) -> str: ...

    def get_json(self, key: str) -> object | None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClarificationStore:
    """Create-only persistence for :class:`ClarificationRecord`s (R2.2).

    Each issued clarification is written under ``clarifications/{id}.json`` with
    a create-only precondition, giving the record immutability for free: the id
    is minted fresh per issue, so the write always creates rather than mutates.
    """

    def __init__(self, store: JsonStore, settings: Settings):
        self._store = store
        self._settings = settings

    def issue(
        self,
        *,
        original_question: str,
        conversation_turn_id: str,
        clarification_question: str,
        document_scope: list[str] | None = None,
    ) -> ClarificationPrompt:
        """Mint, persist, and return a clarification prompt.

        Binds a fresh unguessable ``clarification_id`` to the originating turn,
        the document scope, the original question, and an expiry computed from
        ``clarification_expiry_minutes`` (R2.2).
        """
        clarification_id = secrets.token_urlsafe(_CLARIFICATION_ID_BYTES)
        expiry = _utcnow() + timedelta(
            minutes=self._settings.clarification_expiry_minutes
        )
        expiry_iso = expiry.isoformat()

        record = ClarificationRecord(
            clarification_id=clarification_id,
            conversation_turn_id=conversation_turn_id,
            original_question=original_question,
            document_scope=document_scope,
            clarification_expiry=expiry_iso,
        )
        # Create-only: a second write to the same key would fail, but the id is
        # freshly minted so this always creates the immutable record.
        self._store.create_json(clarification_key(clarification_id), record.model_dump())

        logger.info(
            "Issued clarification %s (expires %s, scope=%s)",
            clarification_id,
            expiry_iso,
            "selected" if document_scope else "all",
            extra={
                "clarification_id": clarification_id,
                "conversation_turn_id": conversation_turn_id,
            },
        )
        metrics.increment("rag_clarifications_issued_total")

        return ClarificationPrompt(
            clarification_question=clarification_question,
            clarification_id=clarification_id,
            conversation_turn_id=conversation_turn_id,
            clarification_expiry=expiry_iso,
            document_scope=document_scope,
        )

    def load(self, clarification_id: str) -> ClarificationRecord:
        """Read and validate the record for ``clarification_id`` (R2.5).

        Rejects an unknown id (nothing stored under the id-derived key) or one
        whose ``clarification_expiry`` has passed with
        :class:`ClarificationInvalidOrExpiredError`. A stored record with an
        unparseable expiry is treated as invalid rather than trusted.
        """
        if not clarification_id:
            raise ClarificationInvalidOrExpiredError(
                "The clarification is invalid or has expired."
            )

        payload = self._store.get_json(clarification_key(clarification_id))
        if payload is None:
            raise ClarificationInvalidOrExpiredError(
                "The clarification is invalid or has expired."
            )

        try:
            record = ClarificationRecord.model_validate(payload)
        except Exception as exc:  # corrupt/legacy payload — treat as invalid
            raise ClarificationInvalidOrExpiredError(
                "The clarification is invalid or has expired."
            ) from exc

        if _is_expired(record.clarification_expiry):
            logger.info(
                "Rejected expired clarification %s (expired %s)",
                clarification_id,
                record.clarification_expiry,
                extra={"clarification_id": clarification_id},
            )
            metrics.increment("rag_clarification_replies_rejected_total", {"reason": "expired"})
            raise ClarificationInvalidOrExpiredError(
                "The clarification is invalid or has expired."
            )

        return record


def resolve_clarification_question(
    *,
    scope_ambiguous: bool,
    clarification_question: str | None,
) -> str:
    """Pick the single clarifying question to ask.

    Scope ambiguity (R2.9) always uses the fixed selected-vs-corpus wording;
    otherwise the classifier's own question is used, falling back to a generic
    prompt when it supplied none.
    """
    if scope_ambiguous:
        return SCOPE_CLARIFICATION_QUESTION
    question = (clarification_question or "").strip()
    return question or DEFAULT_CLARIFICATION_QUESTION


# ---------------------------------------------------------------------------
# Reply processing (R2.4-R2.8)
# ---------------------------------------------------------------------------

#: Description surfaced when a clarification reply still leaves the ambiguity
#: unresolved (R2.8). Kept within the 1..1000 char bound of
#: :class:`AbstentionResponse`.
_UNRESOLVED_MISSING_INFORMATION = (
    "Your reply did not resolve the ambiguity in the original question, so the "
    "system cannot answer it confidently. Try asking a more specific question."
)


def _is_expired(expiry_iso: str, *, now: datetime | None = None) -> bool:
    """Return whether the ISO-8601 ``expiry_iso`` is in the past (R2.5).

    An unparseable timestamp is treated as expired (fail closed) rather than
    trusted as valid.
    """
    try:
        expiry = datetime.fromisoformat(expiry_iso)
    except (TypeError, ValueError):
        return True
    # Records are written tz-aware, but tolerate a naive stored value by
    # interpreting it as UTC so the comparison never raises.
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (now or _utcnow()) >= expiry


def combine_question_and_reply(original_question: str, reply: str) -> str:
    """Combine the original question with the clarification reply (R2.4)."""
    return f"{original_question.strip()}\n\nClarification: {reply.strip()}"


class AnswerPath(Protocol):
    """The answer path re-invoked with the clarified question (R2.4).

    Task 5.1 supplies an implementation closing over the router, e.g.
    ``router.query(UnifiedQueryRequest(question=..., document_ids=scope),
    allow_clarification=False)``. Disabling clarification guarantees at most one
    clarification per original question (R2.7); any residual clarification is
    converted to an abstention by the processor (R2.8).
    """

    def __call__(
        self, *, question: str, document_scope: list[str] | None
    ) -> UnifiedQueryResponse | ClarificationPrompt | AbstentionResponse: ...


#: The two things a processed reply can resolve to: a real answer or an
#: abstention. It never resolves to a further :class:`ClarificationPrompt`
#: (R2.8).
ReplyOutcome = UnifiedQueryResponse | AbstentionResponse


class ClarificationReplyProcessor:
    """Validates a clarification reply and re-runs the answer path (R2.4-R2.8).

    Composes the persistence side (:class:`ClarificationStore`, for
    existence/expiry validation) with an injected :class:`AnswerPath` so this
    logic stays decoupled from the ``/ask/clarify`` endpoint wiring (task 5.1)
    and the concrete router.
    """

    def __init__(self, store: ClarificationStore, answer_path: AnswerPath):
        self._store = store
        self._answer_path = answer_path

    def process(self, *, clarification_id: str, reply: str) -> ReplyOutcome:
        """Process a reply to a previously issued clarification.

        Validates (in order) the referenced record's existence and expiry (R2.5)
        and that the reply is non-empty (R2.6); then re-runs the answer path with
        the original question combined with the reply, scoped to the record's
        ``document_scope`` and with the ambiguous branch disabled (R2.4, R2.7).
        If the ambiguity is still unresolved it returns an
        :class:`AbstentionResponse` and never a further clarification (R2.8).

        Raises:
            ClarificationInvalidOrExpiredError: unknown or expired id (R2.5).
            ClarificationReplyRequiredError: empty/whitespace-only reply (R2.6).
        """
        # R2.5 — existence + expiry (raises on failure).
        record = self._store.load(clarification_id)

        # R2.6 — a non-empty reply is required.
        if not reply or not reply.strip():
            metrics.increment(
                "rag_clarification_replies_rejected_total", {"reason": "empty"}
            )
            raise ClarificationReplyRequiredError("A clarification reply is required.")

        combined_question = combine_question_and_reply(record.original_question, reply)

        logger.info(
            "Processing clarification reply for %s (scope=%s)",
            clarification_id,
            "selected" if record.document_scope else "all",
            extra={
                "clarification_id": clarification_id,
                "conversation_turn_id": record.conversation_turn_id,
            },
        )
        metrics.increment("rag_clarification_replies_processed_total")

        # R2.4 / R2.7 — re-run scoped to the record's document scope with the
        # ambiguous branch disabled (the AnswerPath is responsible for passing
        # ``allow_clarification=False``).
        outcome = self._answer_path(
            question=combined_question, document_scope=record.document_scope
        )

        # R2.8 — a clarification reply must never yield a further clarification;
        # if the answer path still cannot resolve the ambiguity, abstain.
        if isinstance(outcome, ClarificationPrompt):
            logger.info(
                "Clarification %s still ambiguous after reply — abstaining (R2.8)",
                clarification_id,
                extra={"clarification_id": clarification_id},
            )
            metrics.increment(
                "rag_clarification_replies_unresolved_total", {"reason": "still_ambiguous"}
            )
            return _unresolved_abstention(outcome.conversation_turn_id)

        return outcome


def _unresolved_abstention(trace_hint: str | None = None) -> AbstentionResponse:
    """Build the abstention returned when a reply leaves ambiguity unresolved (R2.8)."""
    trace_id = get_trace_id() or trace_hint or str(uuid.uuid4())
    return AbstentionResponse(
        reason_code=ReasonCode.low_confidence,
        missing_information=_UNRESOLVED_MISSING_INFORMATION,
        trace_id=trace_id,
    )
