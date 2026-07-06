"""AI trace investigator (R10).

This module diagnoses an *unsuccessful* recorded query by reading its enriched
:class:`~rag_system.models.QueryTraceRecord` (task 1.7) and producing a
:class:`~rag_system.models.TraceDiagnosis` — a cause description plus advisory
recommendations. It is **read-only**: it never mutates AI configuration or the
corpus, and invokes no mutation endpoints (R10.6, R10.7). Recommendations are
suggestions the operator may choose to act on.

Behaviour (R10.1–R10.5)
-----------------------
* The recorded trace is loaded through an injected resolver. If no trace is
  recorded for the id, no diagnosis is performed and
  :class:`TraceNotFoundError` is raised (R10.2).
* The investigator analyzes the trace's ``route``, retrieval scores (from
  ``retrieved_hits``), retrieval order (the ordering of ``retrieved_hits``), and
  the generation outcome (``claims`` / ``evidence_status`` /
  ``abstention_reason_code``) (R10.1).
* When a cause is identified, the diagnosis carries a cause description that
  references at least one analyzed element and between 1 and 10 recommended
  changes, each targeting the AI configuration or the corpus (R10.3, R10.5).
* When no cause can be determined, the diagnosis carries a "no cause
  determined" description and zero recommendations (R10.4).

Engine
------
Diagnosis is LLM-based, using the dedicated ``trace_investigator_model_id``
(``gemini-3.1-pro``, a thinking model) with a bounded
``trace_investigator_thinking_budget`` and its own
``trace_investigator_read_timeout_s`` — configured independently of the
generation and judge models. The model call is injected as a
:class:`~rag_system.llm.TextLLM`, so tests stub the model output and verify the
cause/recommendation shaping deterministically. Any model error, timeout, or
unparseable output degrades to a safe "no cause determined" diagnosis (zero
recommendations) rather than raising.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from rag_system.config import Settings
from rag_system.llm import TextLLM, build_text_llm
from rag_system.models import QueryTraceRecord, Recommendation, TraceDiagnosis
from rag_system.observability import get_logger

logger = get_logger(__name__)

__all__ = [
    "TraceInvestigator",
    "TraceNotFoundError",
    "TraceResolver",
]

#: Resolves the recorded :class:`QueryTraceRecord` for a trace id, or ``None``
#: when no trace is recorded (R10.2).
TraceResolver = Callable[[str], QueryTraceRecord | None]

#: The four elements the investigator analyzes (R10.1); a cause description must
#: reference at least one of these (R10.3).
_ANALYZED_ELEMENTS = ("route", "retrieval_scores", "retrieval_order", "generation_outcome")

#: Valid recommendation targets (R10.5).
_TARGETS = ("ai_configuration", "corpus")

#: Upper bound on recommendations returned with an identified cause (R10.5).
_MAX_RECOMMENDATIONS = 10

#: Description returned when no cause could be determined (R10.4).
_NO_CAUSE_DESCRIPTION = (
    "No cause was determined for this query outcome from the recorded trace."
)


class TraceNotFoundError(Exception):
    """Raised when a diagnosis is requested for a trace that is not recorded.

    The endpoint layer maps this to a ``404 trace_not_found`` (R10.2). No
    diagnosis is produced and nothing is mutated.
    """

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        super().__init__(f"Trace not found: {trace_id}")


class TraceInvestigator:
    """Diagnoses an unsuccessful recorded query (R10).

    Args:
        settings: Application settings carrying the trace-investigator model id,
            thinking budget, and read timeout.
        trace_resolver: Resolves the recorded :class:`QueryTraceRecord` for a
            trace id, or ``None`` when absent (R10.2).
        llm: Optional text LLM used for diagnosis. Injecting a stub keeps the
            cause/recommendation shaping deterministic in tests. When omitted, a
            Gemini client configured for the trace investigator is built.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        trace_resolver: TraceResolver,
        llm: TextLLM | None = None,
    ) -> None:
        self._settings = settings
        self._resolve_trace = trace_resolver
        self._llm = llm if llm is not None else _build_investigator_llm(settings)
        self._thinking_budget = settings.trace_investigator_thinking_budget

    def diagnose(self, trace_id: str) -> TraceDiagnosis:
        """Diagnose the recorded trace *trace_id* (R10.1–R10.5, R10.7).

        Loads the recorded trace (raising :class:`TraceNotFoundError` when it is
        not recorded, R10.2), analyzes it, and returns a read-only
        :class:`TraceDiagnosis`. No configuration or corpus mutation is
        performed (R10.7).
        """
        trace = self._resolve_trace(trace_id)
        if trace is None:
            # R10.2: no diagnosis for an unrecorded trace; the caller maps this
            # to a 404. Nothing is mutated.
            raise TraceNotFoundError(trace_id)

        prompt = _build_diagnosis_prompt(trace)
        try:
            raw, _usage = self._llm.generate(
                prompt,
                temperature=0.0,
                max_tokens=2048,
                thinking_budget=self._thinking_budget,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the diagnosis request
            logger.warning(
                "Trace investigator model call failed for %s; returning no-cause "
                "diagnosis: %s",
                trace_id,
                exc,
            )
            return _no_cause_diagnosis(trace.trace_id)

        return _parse_diagnosis(raw, trace)


# ---------------------------------------------------------------------------
# Default LLM construction
# ---------------------------------------------------------------------------


def _build_investigator_llm(settings: Settings) -> TextLLM:
    """Build a Gemini client configured for the trace investigator (R10).

    The investigator uses its own model id and read timeout, tuned
    independently of the generation and judge models, so the base ``Settings``
    are copied with those two knobs overridden before constructing the client.
    """
    investigator_settings = settings.model_copy(
        update={
            "gemini_model_id": settings.trace_investigator_model_id,
            "gemini_read_timeout_s": settings.trace_investigator_read_timeout_s,
        }
    )
    return build_text_llm(investigator_settings)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_diagnosis_prompt(trace: QueryTraceRecord) -> str:
    """Render the diagnosis prompt from the recorded trace (R10.1).

    Surfaces the four analyzed elements — route, retrieval scores, retrieval order,
    and generation outcome — so the model reasons over exactly the recorded
    signals.
    """
    hits = trace.retrieved_hits
    retrieval_lines: list[str] = []
    for rank, hit in enumerate(hits):
        retrieval_lines.append(
            f"  {rank + 1}. document={hit.document_id} chunk={hit.chunk_id} "
            f"score={hit.score:.4f} source={hit.source}"
        )
    retrieval_block = "\n".join(retrieval_lines) if retrieval_lines else "  (no hits retrieved)"

    claim_lines: list[str] = []
    for claim in trace.claims:
        claim_lines.append(f"  - [{claim.evidence_status}] {claim.text}")
    claim_block = "\n".join(claim_lines) if claim_lines else "  (no claims decomposed)"

    return (
        "You are an AI trace investigator for an enterprise RAG system. An "
        "operator has asked you to diagnose why a recorded query was "
        "unsuccessful. Analyze ONLY the recorded trace below and reason over its "
        "route, retrieval scores, retrieval order, and generation outcome.\n"
        "\n"
        f"Question: {trace.question}\n"
        f"Route: {trace.route}\n"
        f"Retrieval mode: {trace.retrieval_mode}\n"
        f"Confidence: {trace.confidence} (score={trace.confidence_score})\n"
        f"Evidence status: {trace.evidence_status}\n"
        f"Abstention reason code: {trace.abstention_reason_code}\n"
        f"SQL: {trace.sql}\n"
        "\n"
        "Retrieval hits (in retrieval order, best first):\n"
        f"{retrieval_block}\n"
        "\n"
        "Generation outcome (decomposed claims and evidence status):\n"
        f"{claim_block}\n"
        "\n"
        "Answer text:\n"
        f"{trace.answer}\n"
        "\n"
        "Diagnose the most likely cause of the unsuccessful outcome. Your cause "
        "description MUST reference at least one analyzed element: the route, the "
        "retrieval scores, the retrieval order, or the generation outcome. If you "
        "identify a cause, recommend between 1 and 10 concrete changes; each "
        "recommendation must target either the AI configuration "
        '("ai_configuration") or the corpus ("corpus"). If you cannot determine '
        "a cause, say so explicitly and recommend nothing. Recommend only — never "
        "assume any change will be applied automatically.\n"
        "\n"
        "Return ONLY valid JSON with no markdown formatting:\n"
        '{"cause_description": "one or two sentences", '
        '"analyzed_elements": ["route" | "retrieval_scores" | "retrieval_order" | '
        '"generation_outcome"], '
        '"recommendations": [{"target": "ai_configuration" | "corpus", '
        '"description": "one sentence"}]}'
    )


# ---------------------------------------------------------------------------
# Response parsing / shaping
# ---------------------------------------------------------------------------


def _parse_diagnosis(raw: str, trace: QueryTraceRecord) -> TraceDiagnosis:
    """Parse and shape the model's diagnosis into a valid :class:`TraceDiagnosis`.

    Enforces the R10 output contract regardless of exactly what the model
    returned:

    * unparseable output → a safe "no cause determined" diagnosis (R10.4);
    * recommendations filtered to valid targets and clamped to at most 10, with
      analyzed elements guaranteed non-empty when a cause is present
      (R10.3, R10.5);
    * an empty recommendation set → a "no cause determined" description and zero
      recommendations (R10.4).
    """
    payload = _extract_json_object(raw)
    if payload is None:
        logger.warning(
            "Trace investigator returned unparseable output for %s; "
            "returning no-cause diagnosis",
            trace.trace_id,
        )
        return _no_cause_diagnosis(trace.trace_id)

    recommendations = _parse_recommendations(payload.get("recommendations"))
    cause_description = str(payload.get("cause_description", "")).strip()

    # No cause path (R10.4): zero recommendations and a description that says so.
    if not recommendations:
        return _no_cause_diagnosis(trace.trace_id, cause_description)

    # Identified-cause path (R10.5): 1..10 recommendations.
    recommendations = recommendations[:_MAX_RECOMMENDATIONS]

    analyzed_elements = _parse_analyzed_elements(payload.get("analyzed_elements"))
    # R10.3: the cause must reference at least one analyzed element. If the model
    # omitted them, fall back to the elements actually available in the trace.
    if not analyzed_elements:
        analyzed_elements = _available_elements(trace)

    if not cause_description:
        cause_description = "A cause was identified from the recorded trace."

    return TraceDiagnosis(
        trace_id=trace.trace_id,
        cause_description=cause_description,
        analyzed_elements=analyzed_elements,
        recommendations=recommendations,
    )


def _parse_recommendations(value: Any) -> list[Recommendation]:
    """Coerce the raw ``recommendations`` field into valid recommendations.

    Drops entries whose target is not ``ai_configuration``/``corpus`` or whose
    description is empty (R10.5).
    """
    if not isinstance(value, list):
        return []
    recommendations: list[Recommendation] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target", "")).strip()
        description = str(entry.get("description", "")).strip()
        if target not in _TARGETS or not description:
            continue
        recommendations.append(Recommendation(target=target, description=description))  # type: ignore[arg-type]
    return recommendations


def _parse_analyzed_elements(value: Any) -> list[str]:
    """Filter the raw ``analyzed_elements`` to the valid literal set (R10.3).

    Order is preserved and duplicates are removed.
    """
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    elements: list[str] = []
    for entry in value:
        element = str(entry).strip()
        if element in _ANALYZED_ELEMENTS and element not in seen:
            seen.add(element)
            elements.append(element)
    return elements


def _available_elements(trace: QueryTraceRecord) -> list[str]:
    """Return the analyzed elements present in the recorded trace (R10.1, R10.3).

    Used as a fallback so an identified cause always references at least one
    analyzed element even when the model omits them. ``route`` is always
    available; the others depend on what the trace recorded.
    """
    elements = ["route"]
    if trace.retrieved_hits:
        elements.append("retrieval_scores")
        if len(trace.retrieved_hits) > 1:
            elements.append("retrieval_order")
    if trace.claims or trace.abstention_reason_code is not None or trace.answer:
        elements.append("generation_outcome")
    return elements


def _no_cause_diagnosis(trace_id: str, description: str = "") -> TraceDiagnosis:
    """Build a "no cause determined" diagnosis with zero recommendations (R10.4)."""
    return TraceDiagnosis(
        trace_id=trace_id,
        cause_description=description or _NO_CAUSE_DESCRIPTION,
        analyzed_elements=[],
        recommendations=[],
    )


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response, or ``None``.

    Handles markdown-fenced JSON and JSON embedded in prose, mirroring the
    router's tolerant parsing.
    """
    if not raw or not raw.strip():
        return None
    stripped = raw.strip()
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
        return None
    if not isinstance(payload, dict):
        return None
    return payload
