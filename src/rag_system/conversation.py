"""Server-side multi-turn conversation support.

Turns the stateless ``/ask`` pipeline into a copilot: conversations are stored
server-side, follow-up questions are rewritten into standalone queries against
the stored history before routing/retrieval, and the selected-document scope is
carried across turns. The rewritten query is surfaced back to the caller for
transparency.

The :class:`ConversationManager` is the single integration point used by the
router. It is deliberately tolerant: any failure to load, rewrite, or persist a
conversation degrades to the plain single-turn behaviour rather than failing the
user's query.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from rag_system.config import Settings
from rag_system.llm import build_text_llm
from rag_system.models import (
    ConversationRecord,
    ConversationTurn,
    UnifiedQueryRequest,
)
from rag_system.observability import get_logger, metrics
from rag_system.storage import conversation_key

logger = get_logger(__name__)

#: Answers can be long; only their opening is useful as rewrite context, so each
#: prior answer is truncated to this many characters in the prompt.
_ANSWER_CONTEXT_CHARS = 500


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonStore(Protocol):
    """The slice of :class:`S3ArtifactStore` this module needs."""

    def get_json(self, key: str) -> object | None: ...

    def put_json(self, key: str, payload: object) -> str: ...


@dataclass
class PreparedTurn:
    """The result of preparing a request for a conversation turn.

    ``effective_request`` carries the standalone (possibly rewritten) question
    and the resolved document scope, and is what the router should route and
    retrieve against. ``rewritten_question`` is non-``None`` only when the
    follow-up was actually rewritten, so callers can surface it for transparency.
    """

    conversation: ConversationRecord
    effective_request: UnifiedQueryRequest
    original_question: str
    rewritten_question: str | None
    forget: bool

    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id


# ---------------------------------------------------------------------------
# Follow-up rewriting
# ---------------------------------------------------------------------------


class FollowUpRewriter:
    """Rewrites a follow-up question into a standalone query via the LLM."""

    def __init__(self, settings: Settings, llm: Any | None = None):
        self._llm = llm if llm is not None else build_text_llm(settings)
        self._model_id = getattr(self._llm, "model_id", "unknown")
        self._window = getattr(settings, "conversation_rewrite_window", 6)

    def rewrite(self, question: str, prior_turns: list[ConversationTurn]) -> str:
        """Return a standalone version of ``question`` given the recent turns.

        Falls back to the original question when there is no history or the
        model output cannot be parsed — never raises for a bad LLM response.
        """
        if not prior_turns:
            return question
        window = prior_turns[-self._window :]
        prompt = _build_rewrite_prompt(question, window)
        raw, _usage = self._llm.generate(prompt, temperature=0.0, max_tokens=512)
        return _parse_rewrite_response(raw, fallback=question)


# ---------------------------------------------------------------------------
# Conversation manager
# ---------------------------------------------------------------------------


class ConversationManager:
    """Loads, rewrites, and persists multi-turn conversation state."""

    def __init__(
        self,
        *,
        store: JsonStore,
        settings: Settings,
        rewriter: FollowUpRewriter | None = None,
    ):
        self._store = store
        self._settings = settings
        self._max_turns = getattr(settings, "conversation_max_turns", 12)
        self._rewrite_enabled = getattr(settings, "conversation_rewrite_enabled", True)
        self._rewriter = rewriter if rewriter is not None else FollowUpRewriter(settings)

    # -- Reads ---------------------------------------------------------------

    def load(self, conversation_id: str) -> ConversationRecord | None:
        """Return a stored conversation, or ``None`` if missing/corrupt."""
        try:
            payload = self._store.get_json(conversation_key(conversation_id))
        except Exception:  # noqa: BLE001 - a storage hiccup must not break asking
            logger.warning(
                "Failed to load conversation %s; treating as new",
                conversation_id,
                exc_info=True,
            )
            return None
        if payload is None:
            return None
        try:
            return ConversationRecord.model_validate(payload)
        except Exception:  # noqa: BLE001 - tolerate a schema drift / bad record
            logger.warning(
                "Corrupt conversation record %s; starting fresh",
                conversation_id,
                exc_info=True,
            )
            return None

    # -- Turn lifecycle ------------------------------------------------------

    def prepare(self, request: UnifiedQueryRequest) -> PreparedTurn:
        """Resolve the conversation, rewrite the follow-up, and fix the scope."""
        conversation = None
        if request.conversation_id:
            conversation = self.load(request.conversation_id)
        if conversation is None:
            now = _utcnow()
            conversation = ConversationRecord(
                conversation_id=request.conversation_id or uuid.uuid4().hex,
                created_at=now,
                updated_at=now,
                document_ids=request.document_ids,
                turns=[],
            )

        # "forget context" ignores prior turns for this request; scope is kept.
        prior_turns = [] if request.forget_context else list(conversation.turns)

        # An explicit scope on the request wins (and becomes the new default);
        # otherwise inherit the scope carried by the conversation.
        if request.document_ids is not None:
            document_ids = request.document_ids
        else:
            document_ids = conversation.document_ids

        standalone = request.question
        rewritten = False
        if prior_turns and self._rewrite_enabled:
            try:
                candidate = (self._rewriter.rewrite(request.question, prior_turns) or "").strip()
            except Exception:  # noqa: BLE001 - rewrite is best-effort
                logger.warning(
                    "Follow-up rewrite failed; using the question as asked",
                    exc_info=True,
                )
                metrics.increment("rag_conversation_rewrite_failures_total")
                candidate = ""
            if candidate and candidate != request.question.strip():
                standalone = candidate
                rewritten = True
            metrics.increment(
                "rag_conversation_rewrites_total",
                {"rewritten": "true" if rewritten else "false"},
            )

        effective = UnifiedQueryRequest(
            question=standalone,
            document_ids=document_ids,
            include_sql=request.include_sql,
            conversation_id=conversation.conversation_id,
            forget_context=request.forget_context,
        )
        return PreparedTurn(
            conversation=conversation,
            effective_request=effective,
            original_question=request.question,
            rewritten_question=standalone if rewritten else None,
            forget=request.forget_context,
        )

    def record_turn(
        self,
        prepared: PreparedTurn,
        *,
        answer: str,
        route: str,
        trace_id: str,
    ) -> None:
        """Append the completed turn and persist the conversation (best effort)."""
        conv = prepared.conversation
        if prepared.forget:
            # The user chose to forget context on this turn: drop the accumulated
            # history so subsequent follow-ups only see turns from here on.
            conv.turns = []
        conv.turns.append(
            ConversationTurn(
                question=prepared.original_question,
                standalone_question=prepared.effective_request.question,
                answer=answer,
                route=route,
                trace_id=trace_id,
                asked_at=_utcnow(),
            )
        )
        if len(conv.turns) > self._max_turns:
            conv.turns = conv.turns[-self._max_turns :]
        conv.document_ids = prepared.effective_request.document_ids
        conv.updated_at = _utcnow()
        try:
            self._store.put_json(
                conversation_key(conv.conversation_id), conv.model_dump(mode="json")
            )
            metrics.increment(
                "rag_conversation_turns_recorded_total", {"route": str(route)}
            )
        except Exception:  # noqa: BLE001 - persistence failure must not break the answer
            logger.warning(
                "Failed to persist conversation %s; the turn was answered but not saved",
                conv.conversation_id,
                exc_info=True,
            )

    def forget(self, conversation_id: str) -> ConversationRecord | None:
        """Clear a conversation's accumulated turns, preserving its scope.

        Returns the updated record, or ``None`` when the conversation does not
        exist.
        """
        conv = self.load(conversation_id)
        if conv is None:
            return None
        conv.turns = []
        conv.updated_at = _utcnow()
        self._store.put_json(
            conversation_key(conv.conversation_id), conv.model_dump(mode="json")
        )
        metrics.increment("rag_conversation_forgets_total")
        return conv


# ---------------------------------------------------------------------------
# Prompt building + parsing
# ---------------------------------------------------------------------------


def _build_rewrite_prompt(question: str, prior_turns: list[ConversationTurn]) -> str:
    history_lines: list[str] = []
    for turn in prior_turns:
        answer = turn.answer.strip().replace("\n", " ")
        if len(answer) > _ANSWER_CONTEXT_CHARS:
            answer = answer[:_ANSWER_CONTEXT_CHARS].rstrip() + "…"
        history_lines.append(f"User: {turn.standalone_question}")
        history_lines.append(f"Assistant: {answer}")
    history = "\n".join(history_lines)

    return (
        "You rewrite a user's follow-up message into a fully self-contained "
        "question for a document/database search system.\n"
        "\n"
        "Rules:\n"
        "- Resolve pronouns and references (\"it\", \"that\", \"they\", \"the "
        "same\") using the conversation.\n"
        "- Fold in any implied subject or time frame from earlier turns (e.g. a "
        "follow-up \"What about last quarter?\" should name what metric/topic "
        "\"what about\" refers to).\n"
        "- Keep the user's intent and wording where possible; do NOT answer the "
        "question or add facts.\n"
        "- If the follow-up is already self-contained, return it unchanged.\n"
        "\n"
        "Conversation so far:\n"
        f"{history}\n"
        "\n"
        f"Follow-up message: {question}\n"
        "\n"
        "Return ONLY valid JSON, no markdown:\n"
        '{"standalone_question": "the rewritten, self-contained question"}'
    )


def _parse_rewrite_response(raw: str, *, fallback: str) -> str:
    """Extract ``standalone_question`` from the model output; fall back on failure."""
    stripped = (raw or "").strip()
    if not stripped:
        return fallback
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    else:
        obj = re.search(r"\{.*\}", stripped, re.DOTALL)
        if obj:
            stripped = obj.group(0).strip()
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Could not parse rewrite response; using original question")
        return fallback
    candidate = payload.get("standalone_question")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return fallback
