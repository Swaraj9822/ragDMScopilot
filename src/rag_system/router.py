"""Agentic query router — classifies and routes queries to RAG, database copilot, or both."""

from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any, Iterator

from pydantic import BaseModel, Field

from rag_system.config import Settings
from rag_system.confidence import combine_confidence_scores
from rag_system.copilot import DatabaseCopilotService
from rag_system.llm import build_text_llm
from rag_system.models import (
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
    ):
        self._settings = settings
        self._llm = build_text_llm(settings)
        self._model_id = self._llm.model_id
        self._classifier = BedrockQueryClassifier(settings)
        self._rag = rag_service
        self._copilot = copilot_service
        self._copilot_available = copilot_service is not None

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
    # Public entry point
    # ------------------------------------------------------------------

    def query(self, request: UnifiedQueryRequest) -> UnifiedQueryResponse:
        trace_id = get_trace_id() or str(uuid.uuid4())
        log_extra: dict[str, Any] = {"trace_id": trace_id, "query_len": len(request.question)}

        # Own the per-request query summary so the nested RAG/copilot calls don't
        # each record their own; exactly one summary lands on this /ask trace.
        with unified_query_scope():
            with timed(logger, "query classification", **log_extra):
                decision = self._classifier.classify(request.question, self._table_names)

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
                response = self._route_rag(request, decision, log_extra)
            elif decision.route == QueryRoute.database:
                response = self._route_database(request, decision, log_extra)
            else:
                response = self._route_hybrid(request, decision, trace_id, log_extra)

        record_query_summary(request.question, response.confidence_score)
        return response

    # ------------------------------------------------------------------
    # Streaming entry point
    # ------------------------------------------------------------------

    def query_stream(self, request: UnifiedQueryRequest) -> Iterator[dict[str, Any]]:
        """Stream a unified answer as Server-Sent-Event-style dicts.

        Yields ``meta`` (routing decision), ``status`` (pipeline stage),
        ``delta`` (answer text chunks), and a terminal ``final`` event carrying
        the full :class:`UnifiedQueryResponse` payload. Exactly one query
        summary is recorded for the whole request (the router owns it).
        """
        trace_id = get_trace_id() or str(uuid.uuid4())
        log_extra: dict[str, Any] = {"trace_id": trace_id, "query_len": len(request.question)}
        final_confidence: float | None = None

        with unified_query_scope():
            yield {"type": "status", "stage": "classifying"}
            with timed(logger, "query classification", **log_extra):
                decision = self._classifier.classify(request.question, self._table_names)

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

            yield {
                "type": "meta",
                "trace_id": trace_id,
                "route": str(decision.route),
                "routing_reasoning": decision.reasoning,
            }

            if decision.route == QueryRoute.rag:
                stream = self._stream_rag(request, decision)
            elif decision.route == QueryRoute.database:
                stream = self._stream_database(request, decision)
            else:
                stream = self._stream_hybrid(request, decision, trace_id)

            for event in stream:
                if event.get("type") == "final":
                    final_confidence = event.get("confidence_score")
                yield event

        record_query_summary(request.question, final_confidence)

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

        return UnifiedQueryResponse(
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
        f"Classify the following question into exactly one route.\n"
        f"\n"
        f"Question: {question}\n"
        f"\n"
        "Return ONLY valid JSON with no markdown formatting:\n"
        '{"route": "rag" | "database" | "hybrid", "reasoning": "one sentence explanation", '
        '"confidence": 0.0 to 1.0}'
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
        return RoutingDecision(
            route=QueryRoute(route),
            reasoning=str(payload.get("reasoning", "Classified by LLM")),
            confidence=float(payload.get("confidence", 0.8)),
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
