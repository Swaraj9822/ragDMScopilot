"""Agentic query router — classifies and routes queries to RAG, database copilot, or both."""

from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any, Iterator

from pydantic import BaseModel, Field

from rag_system.abstention import evaluate_abstention
from rag_system.config import Settings
from rag_system.confidence import combine_confidence_scores
from rag_system.clarification import ClarificationStore, resolve_clarification_question
from rag_system.conversation import ConversationManager, PreparedTurn
from rag_system.copilot import DatabaseCopilotService
from rag_system.llm import build_text_llm
from rag_system.models import (
    AbstentionResponse,
    ClarificationPrompt,
    CopilotQueryRequest,
    QueryRequest,
    UnifiedQueryRequest,
    UnifiedQueryResponse,
)
from rag_system.observability import (
    get_logger,
    get_trace_id,
    metrics,
    retry_on_transient,
    timed,
    unified_query_scope,
)
from rag_system.observability_tracing import record_query_summary
from rag_system.observability_tracing.context import propagate_into_thread

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Routing data types
# ---------------------------------------------------------------------------


class QueryRoute(StrEnum):
    rag = "rag"
    database = "database"
    hybrid = "hybrid"


class RoutingDecision(BaseModel):
    route: QueryRoute
    reasoning: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    #: R2.1 — the question is too ambiguous to answer and needs one focused
    #: clarifying question first. ``route`` still carries the best-guess route
    #: to use once the ambiguity is resolved (or as a fallback).
    ambiguous: bool = Field(default=False)
    #: R2.9 — the ambiguity is specifically about which document scope to
    #: search (the caller's selected documents vs. the entire corpus).
    scope_ambiguous: bool = Field(default=False)
    #: The single focused question to ask when ``ambiguous`` is set. ``None``
    #: lets the caller fall back to a default clarifying question.
    clarification_question: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# LLM-based query classifier
# ---------------------------------------------------------------------------


class BedrockQueryClassifier:
    """Uses the configured LLM provider to classify a query into a routing category."""

    def __init__(self, settings: Settings):
        self._llm = build_text_llm(settings)
        self._model_id = self._llm.model_id

    @retry_on_transient()
    def classify(
        self,
        question: str,
        available_tables: list[str] | None = None,
    ) -> RoutingDecision:
        prompt = _build_classification_prompt(question, available_tables or [])

        logger.info(
            "Classifying query for routing (%d chars)",
            len(question),
            extra={"query_len": len(question), "model_id": self._model_id},
        )

        # Reasoning Gemini models spend part of max_output_tokens on internal
        # "thinking"; a tight cap (e.g. 256) can leave no room for the final
        # JSON and yield an empty response. Give the classifier generous
        # headroom — the visible answer is still tiny.
        raw, usage = self._llm.generate(prompt, temperature=0.0, max_tokens=2048)
        if not raw.strip():
            logger.warning(
                "Classifier returned empty text — defaulting to RAG (usage=%s)",
                usage,
                extra={"model_id": self._model_id},
            )
        decision = _parse_routing_response(raw)

        logger.info(
            "Query routed to '%s' (confidence=%.2f): %s",
            decision.route,
            decision.confidence,
            decision.reasoning,
            extra={
                "route": decision.route,
                "confidence": decision.confidence,
                "model_id": self._model_id,
            },
        )
        metrics.increment("rag_routing_decisions_total", {"route": decision.route})
        return decision


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AgenticRouter:
    """Orchestrates query routing between RAG and database copilot."""

    def __init__(
        self,
        settings: Settings,
        rag_service: Any,  # RagService — typed as Any to avoid circular import
        copilot_service: DatabaseCopilotService | None,
        conversations: ConversationManager | None = None,
        clarifications: ClarificationStore | None = None,
    ):
        self._settings = settings
        self._llm = build_text_llm(settings)
        self._model_id = self._llm.model_id
        self._classifier = BedrockQueryClassifier(settings)
        self._rag = rag_service
        self._copilot = copilot_service
        self._copilot_available = copilot_service is not None
        # Multi-turn state. When absent the router behaves statelessly (each
        # request is a fresh, independent turn), preserving single-turn callers.
        self._conversations = conversations
        # Ambiguity clarification (R2). Built lazily from the RAG service's
        # shared artifact store when not injected, so tests can supply a fake.
        self._clarifications = clarifications

        # Pre-load table names so the classifier prompt knows what data is available
        self._table_names: list[str] = []
        if self._copilot_available:
            try:
                self._table_names = sorted(copilot_service.catalog.table_names)
            except Exception:
                logger.warning("Could not load copilot schema catalog for router")

        logger.info(
            "AgenticRouter initialised (copilot=%s, tables=%d)",
            "available" if self._copilot_available else "unavailable",
            len(self._table_names),
        )

    # ------------------------------------------------------------------
    # Conversation preparation
    # ------------------------------------------------------------------

    def _prepare_conversation(
        self, request: UnifiedQueryRequest
    ) -> tuple[UnifiedQueryRequest, str | None, str | None, PreparedTurn | None]:
        """Resolve multi-turn context for a request.

        Returns ``(effective_request, conversation_id, rewritten_question,
        prepared)``. When no conversation manager is wired, the request is
        returned unchanged and there is nothing to record.
        """
        if self._conversations is None:
            return request, None, None, None
        prepared = self._conversations.prepare(request)
        return (
            prepared.effective_request,
            prepared.conversation_id,
            prepared.rewritten_question,
            prepared,
        )

    # ------------------------------------------------------------------
    # Ambiguity clarification (R2)
    # ------------------------------------------------------------------

    def _clarification_store(self) -> ClarificationStore | None:
        """Return the clarification store, building it lazily from the RAG
        service's shared artifact store when one was not injected.

        Returns ``None`` (so the caller degrades to normal routing) if no store
        can be resolved rather than failing the user's query.
        """
        if self._clarifications is not None:
            return self._clarifications
        store = getattr(self._rag, "artifact_store", None)
        if store is None:
            return None
        self._clarifications = ClarificationStore(store, self._settings)
        return self._clarifications

    def _issue_clarification(
        self,
        request: UnifiedQueryRequest,
        decision: RoutingDecision,
        conversation_turn_id: str,
        log_extra: dict[str, Any],
    ) -> ClarificationPrompt | None:
        """Persist a clarification record and return the prompt (R2.1, R2.2).

        Returns ``None`` when no clarification store is available or persistence
        fails, so the router degrades to routing the best-guess route instead of
        failing the query.
        """
        store = self._clarification_store()
        if store is None:
            logger.warning(
                "Ambiguous query but no clarification store available — routing anyway",
                extra=log_extra,
            )
            return None

        question = resolve_clarification_question(
            scope_ambiguous=decision.scope_ambiguous,
            clarification_question=decision.clarification_question,
        )
        try:
            return store.issue(
                original_question=request.question,
                conversation_turn_id=conversation_turn_id,
                clarification_question=question,
                document_scope=request.document_ids,
            )
        except Exception:
            logger.exception(
                "Failed to persist clarification — routing anyway", extra=log_extra
            )
            return None

    # ------------------------------------------------------------------
    # Abstention gates (R3.1–R3.6)
    # ------------------------------------------------------------------

    def _evaluate_abstention_for_response(
        self,
        response: UnifiedQueryResponse,
        trace_id: str,
    ) -> AbstentionResponse | None:
        """Run the six-trigger abstention evaluation on a completed response.

        The retrieval and post-generation gates are evaluated together because
        the response already carries the signals (confidence score, claims,
        evidence items, route) produced by the pipeline.
        """
        # For the sql_no_rows gate, check if this is a database route with empty
        # rows.
        sql_row_count: int | None = None
        if response.route == "database":
            sql_row_count = len(response.rows)

        # Gather conflicting claim ids from the claims.
        from rag_system.abstention import has_conflicting_evidence as _has_conflict

        conflicting_ids: set[str] = set()
        for claim in response.claims:
            if _has_conflict(claim):
                conflicting_ids.add(claim.claim_id)

        # Retrieval gates (R3.2 no_evidence, R3.6 retrieval_below_threshold) only
        # apply to the pure ``rag`` route, where retrieval is the sole evidence
        # source. On ``hybrid`` the database side may legitimately answer with no
        # passages, and ``database`` performs no passage retrieval — so we pass
        # ``None`` (gate skipped) for those and let their own signals decide.
        retrieval_scores: list[float] | None = None
        if response.route == "rag":
            retrieval_scores = list(response.retrieval_scores)

        return evaluate_abstention(
            trace_id=trace_id,
            route=response.route,
            confidence_score=response.confidence_score,
            route_min_confidence=getattr(self._settings, "route_min_confidence", 0.0),
            claims=response.claims if response.claims else None,
            conflicting_claim_ids=conflicting_ids if conflicting_ids else None,
            sql_row_count=sql_row_count,
            retrieval_score_threshold=getattr(
                self._settings, "retrieval_score_threshold", 0.0
            ),
            retrieval_scores=retrieval_scores,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def query(
        self,
        request: UnifiedQueryRequest,
        *,
        allow_clarification: bool = True,
    ) -> UnifiedQueryResponse | ClarificationPrompt | AbstentionResponse:
        """Classify and route a query, or ask for clarification when ambiguous.

        Returns a :class:`ClarificationPrompt` (instead of routing) when the
        classifier flags the question as ambiguous and ``allow_clarification``
        is set. Returns an :class:`AbstentionResponse` when a retrieval or
        post-generation gate fires. A clarification reply re-runs this path with
        ``allow_clarification=False`` so at most one clarification is issued per
        original question (R2.7).
        """
        trace_id = get_trace_id() or str(uuid.uuid4())
        effective, conversation_id, rewritten_question, prepared = (
            self._prepare_conversation(request)
        )
        log_extra: dict[str, Any] = {
            "trace_id": trace_id,
            "query_len": len(effective.question),
            "conversation_id": conversation_id,
            "rewritten": rewritten_question is not None,
        }

        # Own the per-request query summary so the nested RAG/copilot calls don't
        # each record their own; exactly one summary lands on this /ask trace.
        with unified_query_scope():
            with timed(logger, "query classification", **log_extra):
                decision = self._classifier.classify(effective.question, self._table_names)

            # Ambiguity gate (R2.1): ask one focused clarifying question instead
            # of guessing — unless this turn is already a clarification reply.
            if allow_clarification and decision.ambiguous:
                conversation_turn_id = (
                    f"{prepared.conversation_id}:{len(prepared.conversation.turns)}"
                    if prepared is not None
                    else trace_id
                )
                prompt = self._issue_clarification(
                    effective, decision, conversation_turn_id, log_extra
                )
                if prompt is not None:
                    return prompt

            # If copilot is unavailable, force RAG route
            if decision.route in (QueryRoute.database, QueryRoute.hybrid) and not self._copilot_available:
                logger.warning(
                    "Copilot unavailable — falling back to RAG",
                    extra={**log_extra, "original_route": decision.route},
                )
                decision = RoutingDecision(
                    route=QueryRoute.rag,
                    reasoning=f"Copilot unavailable; original route was '{decision.route}'. Falling back to document search.",
                    confidence=decision.confidence,
                )

            if decision.route == QueryRoute.rag:
                response = self._route_rag(effective, decision, log_extra)
            elif decision.route == QueryRoute.database:
                response = self._route_database(effective, decision, log_extra)
            else:
                response = self._route_hybrid(effective, decision, trace_id, log_extra)

        # --- Abstention gates (R3.1–R3.6) ---
        # Evaluate post-generation abstention after the pipeline produces a
        # response with claims. Retrieval scores are derived from the response
        # and the route's threshold settings.
        abstention = self._evaluate_abstention_for_response(response, trace_id)
        if abstention is not None:
            logger.info(
                "Abstention gate fired: %s (trace=%s)",
                abstention.reason_code,
                trace_id,
                extra={**log_extra, "reason_code": abstention.reason_code},
            )
            metrics.increment(
                "rag_abstention_total", {"reason_code": abstention.reason_code}
            )
            record_query_summary(effective.question, response.confidence_score)
            return abstention

        response.conversation_id = conversation_id
        response.rewritten_question = rewritten_question
        if prepared is not None:
            self._conversations.record_turn(
                prepared,
                answer=response.answer,
                route=response.route,
                trace_id=response.trace_id,
            )

        record_query_summary(effective.question, response.confidence_score)
        return response

    # ------------------------------------------------------------------
    # Streaming entry point
    # ------------------------------------------------------------------

    def query_stream(self, request: UnifiedQueryRequest) -> Iterator[dict[str, Any]]:
        """Stream a unified answer as Server-Sent-Event-style dicts.

        Emits stage-progress events (``classify``, ``retrieve``, ``generate``,
        ``verify``) for liveness but **holds answer content** — it does not
        forward generated tokens — until the abstention gates and
        claim-verification have run. The stream ends with exactly **one terminal
        event** carrying one of: the answer with claims/evidence, a
        ``Clarification_Prompt``, or an ``Abstention_Response`` (no answer
        content). A post-generation abstention therefore leaks no tokens (R3.7).
        """
        trace_id = get_trace_id() or str(uuid.uuid4())
        effective, conversation_id, rewritten_question, prepared = (
            self._prepare_conversation(request)
        )
        log_extra: dict[str, Any] = {
            "trace_id": trace_id,
            "query_len": len(effective.question),
            "conversation_id": conversation_id,
            "rewritten": rewritten_question is not None,
        }

        with unified_query_scope():
            # --- Stage: classify ---
            yield {"type": "status", "stage": "classify"}
            with timed(logger, "query classification", **log_extra):
                decision = self._classifier.classify(effective.question, self._table_names)

            # Ambiguity gate — if ambiguous, yield one terminal clarification
            # event and end the stream.
            if decision.ambiguous:
                conversation_turn_id = (
                    f"{prepared.conversation_id}:{len(prepared.conversation.turns)}"
                    if prepared is not None
                    else trace_id
                )
                prompt = self._issue_clarification(
                    effective, decision, conversation_turn_id, log_extra
                )
                if prompt is not None:
                    yield {
                        "type": "terminal",
                        "kind": "clarification",
                        "payload": prompt.model_dump(mode="json"),
                        "trace_id": trace_id,
                        "conversation_id": conversation_id,
                    }
                    record_query_summary(effective.question, None)
                    return

            if (
                decision.route in (QueryRoute.database, QueryRoute.hybrid)
                and not self._copilot_available
            ):
                logger.warning(
                    "Copilot unavailable — falling back to RAG",
                    extra={**log_extra, "original_route": decision.route},
                )
                decision = RoutingDecision(
                    route=QueryRoute.rag,
                    reasoning=(
                        f"Copilot unavailable; original route was '{decision.route}'. "
                        "Falling back to document search."
                    ),
                    confidence=decision.confidence,
                )

            # --- Stage: retrieve ---
            yield {"type": "status", "stage": "retrieve"}

            # --- Stage: generate ---
            yield {"type": "status", "stage": "generate"}

            # Execute the full pipeline (which includes retrieve + generate +
            # claim mapping internally) and collect the response WITHOUT
            # forwarding any tokens to the client.
            if decision.route == QueryRoute.rag:
                response = self._route_rag(effective, decision, log_extra)
            elif decision.route == QueryRoute.database:
                response = self._route_database(effective, decision, log_extra)
            else:
                response = self._route_hybrid(effective, decision, trace_id, log_extra)

            # --- Stage: verify (abstention gates + claim verification) ---
            yield {"type": "status", "stage": "verify"}

            abstention = self._evaluate_abstention_for_response(response, trace_id)
            if abstention is not None:
                logger.info(
                    "Abstention gate fired (stream): %s (trace=%s)",
                    abstention.reason_code,
                    trace_id,
                    extra={**log_extra, "reason_code": abstention.reason_code},
                )
                metrics.increment(
                    "rag_abstention_total", {"reason_code": abstention.reason_code}
                )
                yield {
                    "type": "terminal",
                    "kind": "abstention",
                    "payload": abstention.model_dump(mode="json"),
                    "trace_id": trace_id,
                    "conversation_id": conversation_id,
                }
                record_query_summary(effective.question, response.confidence_score)
                return

            # All gates passed — emit the terminal answer event.
            response.conversation_id = conversation_id
            response.rewritten_question = rewritten_question

            if prepared is not None:
                self._conversations.record_turn(
                    prepared,
                    answer=response.answer,
                    route=response.route,
                    trace_id=response.trace_id,
                )

            # Strip SQL/rows from the client-facing payload when not requested.
            if not effective.include_sql:
                response.sql = None
                response.rows = []

            yield {
                "type": "terminal",
                "kind": "answer",
                "payload": response.model_dump(mode="json"),
                "trace_id": trace_id,
                "conversation_id": conversation_id,
            }

        record_query_summary(effective.question, response.confidence_score)

    def _stream_rag(
        self, request: UnifiedQueryRequest, decision: RoutingDecision
    ) -> Iterator[dict[str, Any]]:
        logger.info("Routing to RAG pipeline (streaming)", extra={"route": "rag"})
        rag_request = QueryRequest(question=request.question, document_ids=request.document_ids)
        for event in self._rag.query_stream(rag_request):
            if event.get("type") == "final":
                yield _unified_final_from_rag(event["response"], decision.reasoning)
            else:
                yield event

    def _stream_database(
        self, request: UnifiedQueryRequest, decision: RoutingDecision
    ) -> Iterator[dict[str, Any]]:
        logger.info("Routing to database copilot (streaming)", extra={"route": "database"})
        copilot_request = CopilotQueryRequest(
            question=request.question,
            include_sql=request.include_sql,
        )
        for event in self._copilot.query_stream(copilot_request):
            if event.get("type") == "final":
                yield _unified_final_from_database(event["response"], decision.reasoning)
            else:
                yield event

    def _stream_hybrid(
        self,
        request: UnifiedQueryRequest,
        decision: RoutingDecision,
        trace_id: str,
    ) -> Iterator[dict[str, Any]]:
        logger.info("Routing to hybrid (streaming)", extra={"route": "hybrid"})
        yield {"type": "status", "stage": "gathering"}

        rag_request = QueryRequest(question=request.question, document_ids=request.document_ids)
        copilot_request = CopilotQueryRequest(
            question=request.question,
            include_sql=request.include_sql,
        )

        # RAG and the database copilot are independent — run them concurrently,
        # propagating trace/span context so their spans attach to this trace.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid") as pool:
            rag_fn = propagate_into_thread(lambda: self._rag.query(rag_request))
            copilot_fn = propagate_into_thread(lambda: self._copilot.query(copilot_request))
            rag_future = pool.submit(rag_fn)
            copilot_future = pool.submit(copilot_fn)
            rag_response = rag_future.result()
            copilot_response = copilot_future.result()

        rag_grounded = rag_response.evidence_status == "grounded"
        db_grounded = copilot_response.evidence_status == "grounded"
        if rag_grounded and db_grounded:
            evidence_status = "grounded"
        elif rag_grounded or db_grounded:
            evidence_status = "partially_grounded"
        else:
            evidence_status = "insufficient_evidence"

        if self._should_synthesize_hybrid(
            rag_response.answer, copilot_response.answer
        ):
            yield {"type": "status", "stage": "synthesizing"}
            prompt = _build_synthesis_prompt(
                request.question, rag_response.answer, copilot_response.answer
            )
            parts: list[str] = []
            for piece in self._llm.generate_stream(
                prompt, temperature=0.1, max_tokens=4096
            ):
                parts.append(piece)
                yield {"type": "delta", "text": piece}
            merged_answer = "".join(parts).strip()
            metrics.increment("rag_hybrid_synthesis_total")
        else:
            yield {"type": "status", "stage": "composing"}
            merged_answer = _compose_hybrid_sections(
                rag_response.answer, copilot_response.answer
            )
            metrics.increment("rag_hybrid_sections_total")
            yield {"type": "delta", "text": merged_answer}

        combined_confidence = combine_confidence_scores(
            [rag_response.confidence_score, copilot_response.confidence_score]
        )
        yield {
            "type": "final",
            "answer": merged_answer,
            "route": "hybrid",
            "evidence_status": evidence_status,
            "trace_id": trace_id,
            "citations": [c.model_dump() for c in rag_response.citations],
            "confidence": rag_response.confidence,
            "confidence_score": combined_confidence,
            "insufficient_evidence_reason": rag_response.insufficient_evidence_reason,
            "sql": copilot_response.sql,
            "rows": copilot_response.rows,
            "data_sources": [d.model_dump() for d in copilot_response.data_sources],
            "routing_reasoning": decision.reasoning,
        }

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    def _route_rag(
        self,
        request: UnifiedQueryRequest,
        decision: RoutingDecision,
        log_extra: dict[str, Any],
    ) -> UnifiedQueryResponse:
        logger.info("Routing to RAG pipeline", extra=log_extra)
        rag_request = QueryRequest(question=request.question, document_ids=request.document_ids)

        with timed(logger, "RAG query (routed)", **log_extra):
            rag_response = self._rag.query(rag_request)

        return UnifiedQueryResponse(
            answer=rag_response.answer,
            route="rag",
            evidence_status=rag_response.evidence_status,
            trace_id=rag_response.trace_id,
            citations=rag_response.citations,
            confidence=rag_response.confidence,
            confidence_score=rag_response.confidence_score,
            insufficient_evidence_reason=rag_response.insufficient_evidence_reason,
            routing_reasoning=decision.reasoning,
            claims=rag_response.claims,
            claim_decomposition_failed=rag_response.claim_decomposition_failed,
            retrieval_scores=rag_response.retrieval_scores,
        )

    def _route_database(
        self,
        request: UnifiedQueryRequest,
        decision: RoutingDecision,
        log_extra: dict[str, Any],
    ) -> UnifiedQueryResponse:
        logger.info("Routing to database copilot", extra=log_extra)
        copilot_request = CopilotQueryRequest(
            question=request.question,
            include_sql=request.include_sql,
        )

        with timed(logger, "copilot query (routed)", **log_extra):
            copilot_response = self._copilot.query(copilot_request)

        response = UnifiedQueryResponse(
            answer=copilot_response.answer,
            route="database",
            evidence_status=copilot_response.evidence_status,
            trace_id=copilot_response.trace_id,
            confidence_score=copilot_response.confidence_score,
            sql=copilot_response.sql,
            rows=copilot_response.rows,
            data_sources=copilot_response.data_sources,
            routing_reasoning=decision.reasoning,
        )

        # The database route has no RagService leaf call to record a trace, so
        # persist one here — otherwise the trace investigator's diagnose endpoint
        # returns ``trace_not_found`` for every database answer (R10.2).
        persist = getattr(self._rag, "persist_unified_query_trace", None)
        if persist is not None:
            persist(
                question=request.question,
                document_ids=request.document_ids,
                response=response,
            )
        return response

    def _route_hybrid(
        self,
        request: UnifiedQueryRequest,
        decision: RoutingDecision,
        trace_id: str,
        log_extra: dict[str, Any],
    ) -> UnifiedQueryResponse:
        logger.info("Routing to hybrid (RAG + database)", extra=log_extra)

        rag_request = QueryRequest(question=request.question, document_ids=request.document_ids)
        copilot_request = CopilotQueryRequest(
            question=request.question,
            include_sql=request.include_sql,
        )

        # RAG and the database copilot are independent lookups — run them
        # concurrently so the shorter branch is hidden under the longer one.
        # propagate_into_thread copies the trace/span context into the worker
        # threads so spans created in each branch attach to the originating
        # trace (R2.3, R2.6).
        with timed(logger, "hybrid parallel lookup (RAG + database)", **log_extra):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid") as pool:
                rag_fn = propagate_into_thread(lambda: self._rag.query(rag_request))
                copilot_fn = propagate_into_thread(
                    lambda: self._copilot.query(copilot_request)
                )
                rag_future = pool.submit(rag_fn)
                copilot_future = pool.submit(copilot_fn)
                rag_response = rag_future.result()
                copilot_response = copilot_future.result()

        # Synthesize a unified answer from both sources — but only when the
        # answers actually overlap (or synthesis is forced on). Otherwise skip
        # the extra LLM call and present labeled sections.
        if self._should_synthesize_hybrid(
            rag_response.answer, copilot_response.answer
        ):
            with timed(logger, "hybrid answer synthesis", **log_extra):
                merged_answer = self._synthesize_hybrid(
                    request.question,
                    rag_response.answer,
                    copilot_response.answer,
                )
        else:
            logger.info(
                "Hybrid sources disjoint — skipping synthesis, composing sections",
                extra=log_extra,
            )
            merged_answer = _compose_hybrid_sections(
                rag_response.answer, copilot_response.answer
            )
            metrics.increment("rag_hybrid_sections_total")

        # Combine evidence statuses
        rag_grounded = rag_response.evidence_status == "grounded"
        db_grounded = copilot_response.evidence_status == "grounded"
        if rag_grounded and db_grounded:
            evidence_status = "grounded"
        elif rag_grounded or db_grounded:
            evidence_status = "partially_grounded"
        else:
            evidence_status = "insufficient_evidence"

        return UnifiedQueryResponse(
            answer=merged_answer,
            route="hybrid",
            evidence_status=evidence_status,
            trace_id=trace_id,
            citations=rag_response.citations,
            confidence=rag_response.confidence,
            confidence_score=combine_confidence_scores(
                [rag_response.confidence_score, copilot_response.confidence_score]
            ),
            insufficient_evidence_reason=rag_response.insufficient_evidence_reason,
            sql=copilot_response.sql,
            rows=copilot_response.rows,
            data_sources=copilot_response.data_sources,
            routing_reasoning=decision.reasoning,
        )

    @retry_on_transient()
    def _synthesize_hybrid(
        self,
        question: str,
        rag_answer: str,
        db_answer: str,
    ) -> str:
        """Merge RAG and database answers into a single coherent response via LLM."""
        prompt = _build_synthesis_prompt(question, rag_answer, db_answer)
        answer, _usage = self._llm.generate(prompt, temperature=0.1, max_tokens=4096)
        metrics.increment("rag_hybrid_synthesis_total")
        return answer

    def _should_synthesize_hybrid(self, rag_answer: str, db_answer: str) -> bool:
        """Decide whether to merge the two answers with an LLM call.

        Synthesis adds ~3s to the critical path, so in the default "auto" mode
        we only pay for it when the two answers actually overlap — otherwise the
        merge has nothing to reconcile and we present labeled sections instead.
        """
        mode = self._settings.hybrid_synthesis_mode
        if mode == "always":
            return True
        if mode == "never":
            return False
        return _answers_overlap(
            rag_answer, db_answer, self._settings.hybrid_overlap_threshold
        )


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def _unified_final_from_rag(response: Any, routing_reasoning: str) -> dict[str, Any]:
    """Build a unified ``final`` event from a RAG QueryResponse."""
    return {
        "type": "final",
        "answer": response.answer,
        "route": "rag",
        "evidence_status": response.evidence_status,
        "trace_id": response.trace_id,
        "citations": [c.model_dump() for c in response.citations],
        "confidence": response.confidence,
        "confidence_score": response.confidence_score,
        "insufficient_evidence_reason": response.insufficient_evidence_reason,
        "sql": None,
        "rows": [],
        "data_sources": [],
        "routing_reasoning": routing_reasoning,
    }


def _unified_final_from_database(response: Any, routing_reasoning: str) -> dict[str, Any]:
    """Build a unified ``final`` event from a CopilotQueryResponse."""
    return {
        "type": "final",
        "answer": response.answer,
        "route": "database",
        "evidence_status": response.evidence_status,
        "trace_id": response.trace_id,
        "citations": [],
        "confidence": None,
        "confidence_score": response.confidence_score,
        "insufficient_evidence_reason": None,
        "sql": response.sql,
        "rows": response.rows,
        "data_sources": [d.model_dump() for d in response.data_sources],
        "routing_reasoning": routing_reasoning,
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_classification_prompt(question: str, available_tables: list[str]) -> str:
    table_section = ""
    if available_tables:
        table_list = ", ".join(available_tables)
        table_section = (
            f"\n   The database copilot has access to these tables: {table_list}.\n"
            "   Use this route for questions about metrics, counts, totals, trends,\n"
            "   aggregations, or any question best answered by querying structured data\n"
            "   in these tables."
        )

    return (
        "You are a query routing classifier for an enterprise AI system with two capabilities:\n"
        "\n"
        "1. **rag** — Retrieves and answers from a corpus of uploaded business documents (PDFs).\n"
        "   Use this route for questions about policies, procedures, report content, document\n"
        "   summaries, or any question best answered by reading document text.\n"
        "\n"
        f"2. **database** — Queries a PostgreSQL database via SQL.{table_section}\n"
        "\n"
        "3. **hybrid** — Use when the question clearly requires BOTH document context AND\n"
        "   database data to produce a complete answer. For example: \"How does our refund\n"
        "   policy compare to actual refund rates this quarter?\"\n"
        "\n"
        "Also decide whether the question is AMBIGUOUS — too under-specified to answer\n"
        "accurately without guessing. If it is, set \"ambiguous\": true and provide a single,\n"
        "focused \"clarification_question\" that would resolve the ambiguity. When the ambiguity\n"
        "is specifically about WHICH DOCUMENTS to search (for example the question could refer\n"
        "to the caller's selected documents or the entire corpus), also set\n"
        "\"scope_ambiguous\": true. Still pick the best-guess route so it can be used once the\n"
        "ambiguity is resolved.\n"
        "\n"
        f"Classify the following question into exactly one route.\n"
        f"\n"
        f"Question: {question}\n"
        f"\n"
        "Return ONLY valid JSON with no markdown formatting:\n"
        '{"route": "rag" | "database" | "hybrid", "reasoning": "one sentence explanation", '
        '"confidence": 0.0 to 1.0, "ambiguous": true | false, "scope_ambiguous": true | false, '
        '"clarification_question": "one focused question or null"}'
    )


# ---------------------------------------------------------------------------
# Hybrid answer composition (overlap detection + labeled sections)
# ---------------------------------------------------------------------------

# Significant tokens are alphanumeric runs of length > 3 that aren't generic
# filler. Kept deliberately small/cheap so the overlap check stays off the
# critical path (no LLM, no allocation beyond two sets).
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "about", "above", "after", "again", "against", "answer", "based",
        "because", "been", "before", "being", "below", "between", "both",
        "data", "does", "doing", "during", "each", "from", "have", "having",
        "here", "into", "more", "most", "only", "other", "over", "same",
        "should", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "under", "until",
        "very", "were", "what", "when", "where", "which", "while", "with",
        "would", "your",
    }
)


def _significant_tokens(text: str) -> set[str]:
    """Lowercased content tokens (len > 3, non-stopword) used for overlap scoring."""
    return {
        token
        for token in _WORD_RE.findall(text.lower())
        if len(token) > 3 and token not in _STOPWORDS
    }


def _answers_overlap(rag_answer: str, db_answer: str, threshold: float) -> bool:
    """Return True when the two answers share enough vocabulary to be worth merging.

    Uses the overlap coefficient (shared tokens / smaller token set) rather than
    Jaccard so a short database answer isn't penalised against a long document
    answer. Empty answers never overlap — there is nothing to reconcile.
    """
    rag_tokens = _significant_tokens(rag_answer)
    db_tokens = _significant_tokens(db_answer)
    if not rag_tokens or not db_tokens:
        return False
    shared = len(rag_tokens & db_tokens)
    coefficient = shared / min(len(rag_tokens), len(db_tokens))
    return coefficient >= threshold


def _compose_hybrid_sections(rag_answer: str, db_answer: str) -> str:
    """Present the document and database answers as two labeled markdown sections.

    Used when synthesis is skipped (sources don't overlap, or synthesis is
    disabled) — avoids the extra merge LLM call while keeping both answers.
    """
    sections: list[str] = []
    rag = rag_answer.strip()
    db = db_answer.strip()
    if rag:
        sections.append(f"## From documents\n\n{rag}")
    if db:
        sections.append(f"## From data\n\n{db}")
    return "\n\n".join(sections)


def _build_synthesis_prompt(question: str, rag_answer: str, db_answer: str) -> str:
    return (
        "You are synthesizing answers from two enterprise data sources into one coherent response.\n"
        "\n"
        f"User question: {question}\n"
        "\n"
        "--- Document-based answer (from business PDFs) ---\n"
        f"{rag_answer}\n"
        "\n"
        "--- Data-based answer (from database query) ---\n"
        f"{db_answer}\n"
        "\n"
        "Write a single, unified answer that integrates insights from both sources.\n"
        "Clearly distinguish between what comes from documents vs. data when relevant.\n"
        "Do not invent facts beyond what the two sources provide."
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _parse_routing_response(raw: str) -> RoutingDecision:
    """Parse the LLM's JSON routing response, with fallback to RAG on failure."""
    stripped = raw.strip()
    # Handle markdown-fenced JSON
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    else:
        # Fall back to the first balanced-looking {...} object, in case the
        # model wraps the JSON in prose or reasoning text.
        obj = re.search(r"\{.*\}", stripped, re.DOTALL)
        if obj:
            stripped = obj.group(0).strip()

    try:
        payload = json.loads(stripped)
        route = payload.get("route", "rag").lower()
        if route not in ("rag", "database", "hybrid"):
            route = "rag"
        scope_ambiguous = bool(payload.get("scope_ambiguous", False))
        # Scope ambiguity is a kind of ambiguity — treat it as ambiguous even if
        # the model only set the narrower flag.
        ambiguous = bool(payload.get("ambiguous", False)) or scope_ambiguous
        clarification_question = payload.get("clarification_question")
        if clarification_question is not None:
            clarification_question = str(clarification_question).strip() or None
        return RoutingDecision(
            route=QueryRoute(route),
            reasoning=str(payload.get("reasoning", "Classified by LLM")),
            confidence=float(payload.get("confidence", 0.8)),
            ambiguous=ambiguous,
            scope_ambiguous=scope_ambiguous,
            clarification_question=clarification_question,
        )
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning(
            "Failed to parse routing response, defaulting to RAG: %s",
            exc,
            extra={"raw_response": raw[:200]},
        )
        return RoutingDecision(
            route=QueryRoute.rag,
            reasoning="Failed to parse classifier output; defaulting to document search.",
            confidence=0.5,
        )
