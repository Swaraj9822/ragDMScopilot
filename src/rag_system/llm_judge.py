"""LLM-as-judge evaluation method (R7.3, R7.6, R7.7, R7.8).

This module scores each ``BenchmarkCase`` with an LLM_Judge that produces a
**faithfulness** score and a **relevance** score, each a numeric value in
``[0.0, 1.0]`` inclusive, whenever LLM scoring is enabled (R7.3). The judge runs
as a *scheduled report* at a configurable interval
(``llm_judge_schedule_interval_hours``) and its scores are **excluded from the
pass/fail determination of any CI run** — CI status is decided solely by the
deterministic method in :mod:`rag_system.evaluation` (R7.6).

Judge model (R7.3, R7.6, R7.8)
------------------------------
The judge deliberately uses a **different, higher tier** model than the
generation model (``gemini-3.5-flash``): ``llm_judge_model_id``
(``gemini-3.1-pro``, a thinking model) on Vertex AI, reusing the existing Vertex
client and credentials. Scoring a generator's output with a distinct model
mitigates self-evaluation bias. Because ``gemini-3.1-pro`` is a thinking model
it is given a **bounded** ``llm_judge_thinking_budget`` and its **own** read
timeout ``llm_judge_read_timeout_s`` (~55s), deliberately shorter than the fixed
``llm_judge_per_case_timeout_s`` (60s) per-case timeout so the per-case timeout
is the outer bound (R7.8).

Timeout handling (R7.8)
-----------------------
Every per-case judge call is wrapped in the fixed 60s per-case timeout. If a
case's scoring does not complete within that timeout — or the model errors or
returns unparseable output — an **error indication is recorded** in
:attr:`~rag_system.models.LLMJudgeScores.error` in place of the scores, and the
case's already-computed **deterministic check outcomes and retrieval metrics are
retained** (R7.8).

The model call is injected as a :class:`~rag_system.llm.TextLLM`, so tests stub
the model output and verify the score shaping and timeout handling
deterministically without live calls.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from rag_system.config import Settings
from rag_system.llm import TextLLM, build_text_llm
from rag_system.models import (
    BenchmarkCase,
    BenchmarkResult,
    LLMJudgeScores,
    QueryResponse,
    QueryTraceRecord,
)
from rag_system.observability import get_logger

logger = get_logger(__name__)

__all__ = [
    "LLMJudge",
    "JudgeTimeoutError",
    "judge_run_due",
    "next_judge_run_at",
]

_T = TypeVar("_T")

#: Recorded in ``LLMJudgeScores.error`` when a case exceeds the per-case timeout.
_TIMEOUT_ERROR = "llm_judge_timeout"

Observed = QueryResponse | QueryTraceRecord


class JudgeTimeoutError(Exception):
    """Raised internally when a per-case judge call exceeds its timeout (R7.8)."""


# ---------------------------------------------------------------------------
# Scheduling (R7.6)
# ---------------------------------------------------------------------------


def judge_run_due(
    last_run_at: datetime | None,
    now: datetime,
    interval_hours: int,
) -> bool:
    """Return whether a scheduled LLM-judge report is due (R7.6).

    A run is due when it has never run (``last_run_at is None``) or when at least
    ``interval_hours`` have elapsed since the last run. This lets a scheduler
    invoke the judge on the configured ``llm_judge_schedule_interval_hours``
    cadence, entirely outside the CI pass/fail path.
    """
    if last_run_at is None:
        return True
    return now - last_run_at >= timedelta(hours=interval_hours)


def next_judge_run_at(last_run_at: datetime | None, interval_hours: int) -> datetime:
    """Return the timestamp of the next scheduled LLM-judge report (R7.6).

    When the judge has never run the next run is *now* (UTC); otherwise it is one
    interval after the last run.
    """
    if last_run_at is None:
        return datetime.now(timezone.utc)
    return last_run_at + timedelta(hours=interval_hours)


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------


class LLMJudge:
    """Scores benchmark cases for faithfulness and relevance (R7.3, R7.7, R7.8).

    Args:
        settings: Application settings carrying the judge model id, thinking
            budget, read timeout, and per-case timeout.
        llm: Optional text LLM used for scoring. Injecting a stub keeps score
            shaping and timeout handling deterministic in tests. When omitted a
            Gemini client configured for the judge (distinct model + shorter read
            timeout) is built.
        per_case_timeout_s: Optional override of the fixed per-case timeout
            (defaults to ``settings.llm_judge_per_case_timeout_s``). Exposed for
            tests so the R7.8 timeout path can be exercised without waiting 60s.
        timeout_runner: Optional strategy for running a call under a timeout,
            raising :class:`JudgeTimeoutError` on breach. Defaults to a
            thread-based runner.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        llm: TextLLM | None = None,
        per_case_timeout_s: float | None = None,
        timeout_runner: Callable[[Callable[[], _T], float], _T] | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm if llm is not None else _build_judge_llm(settings)
        self._thinking_budget = settings.llm_judge_thinking_budget
        self._per_case_timeout_s = (
            per_case_timeout_s
            if per_case_timeout_s is not None
            else float(settings.llm_judge_per_case_timeout_s)
        )
        self._run_with_timeout = timeout_runner or _default_timeout_runner

    def score_case(self, case: BenchmarkCase, observed: Observed) -> LLMJudgeScores:
        """Score a single case's faithfulness and relevance (R7.3, R7.8).

        Returns an :class:`LLMJudgeScores` with both scores in ``[0.0, 1.0]`` on
        success. On per-case timeout, model error, or unparseable output, returns
        scores with :attr:`LLMJudgeScores.error` set and no numeric scores so the
        caller can retain deterministic + retrieval results (R7.8).
        """
        prompt = _build_judge_prompt(case, observed)

        def _call() -> str:
            raw, _usage = self._llm.generate(
                prompt,
                temperature=0.0,
                max_tokens=1024,
                thinking_budget=self._thinking_budget,
            )
            return raw

        try:
            raw = self._run_with_timeout(_call, self._per_case_timeout_s)
        except JudgeTimeoutError:
            logger.warning(
                "LLM judge timed out for case %s after %ss; recording error and "
                "retaining deterministic + retrieval results",
                case.id,
                self._per_case_timeout_s,
            )
            return LLMJudgeScores(error=_TIMEOUT_ERROR)
        except Exception as exc:  # noqa: BLE001 - never fail the evaluation run
            logger.warning(
                "LLM judge model call failed for case %s; recording error: %s",
                case.id,
                exc,
            )
            return LLMJudgeScores(error=f"llm_judge_error: {exc}")

        return _parse_scores(raw, case.id)

    def score_results(
        self,
        cases: Sequence[BenchmarkCase],
        results: Sequence[BenchmarkResult],
        observed_by_case: Mapping[str, Observed],
        *,
        enabled: bool,
    ) -> list[BenchmarkResult]:
        """Attach LLM-judge scores to deterministic results (R7.3, R7.7, R7.8).

        When ``enabled`` is false (LLM scoring disabled) the results are returned
        unchanged with ``llm_judge`` left as ``None`` (R7.3 is gated on *WHERE LLM
        scoring is enabled*). When enabled, each result gains an
        :class:`LLMJudgeScores` while its deterministic checks and retrieval
        metrics are preserved verbatim — so a per-case judge timeout records an
        error indication without discarding the other methods' results (R7.8).

        A case whose observed answer is missing from ``observed_by_case`` records
        an error indication rather than being scored, again preserving the
        deterministic + retrieval results.
        """
        if not enabled:
            return list(results)

        case_by_id = {case.id: case for case in cases}
        scored: list[BenchmarkResult] = []
        for result in results:
            case = case_by_id.get(result.case_id)
            observed = observed_by_case.get(result.case_id)
            if case is None or observed is None:
                judge = LLMJudgeScores(error="llm_judge_missing_observation")
            else:
                judge = self.score_case(case, observed)
            scored.append(
                result.model_copy(update={"llm_judge": judge})
            )
        return scored


# ---------------------------------------------------------------------------
# Default LLM construction (distinct model + shorter read timeout)
# ---------------------------------------------------------------------------


def _build_judge_llm(settings: Settings) -> TextLLM:
    """Build a Gemini client configured for the LLM judge (R7.3, R7.8).

    The judge uses a **distinct, higher-tier** model (``llm_judge_model_id``, not
    ``gemini_model_id``) and its **own** read timeout (``llm_judge_read_timeout_s``,
    deliberately shorter than the generation client's default and the 60s
    per-case timeout), so the base ``Settings`` are copied with those two knobs
    overridden before constructing the client.
    """
    judge_settings = settings.model_copy(
        update={
            "gemini_model_id": settings.llm_judge_model_id,
            "gemini_read_timeout_s": float(settings.llm_judge_read_timeout_s),
        }
    )
    return build_text_llm(judge_settings)


def _default_timeout_runner(fn: Callable[[], _T], timeout_s: float) -> _T:
    """Run ``fn`` under a wall-clock timeout, raising :class:`JudgeTimeoutError`.

    The fixed per-case timeout wraps the whole judge call (R7.8). A thread pool
    is used so a call that overruns is abandoned rather than blocking the run
    indefinitely; the judge's own shorter read timeout should normally trip
    first, with this timeout as the outer bound.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise JudgeTimeoutError from exc


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_judge_prompt(case: BenchmarkCase, observed: Observed) -> str:
    """Render the judge prompt for a case + observed answer (R7.3).

    Surfaces the question, the produced answer, the expected answer (when the
    case carries one), and the cited evidence so the judge can score faithfulness
    (is the answer grounded in the evidence?) and relevance (does the answer
    address the question?).
    """
    evidence_block = _evidence_block(observed)
    expected = case.expected_answer or "(no expected answer provided)"
    return (
        "You are an impartial LLM judge evaluating the answer produced by a "
        "retrieval-augmented generation (RAG) system. Score the answer on two "
        "independent dimensions, each a number from 0.0 to 1.0 inclusive:\n"
        "- faithfulness: how well the answer is grounded in and supported by the "
        "cited evidence below (1.0 = fully grounded, 0.0 = unsupported / "
        "hallucinated).\n"
        "- relevance: how well the answer addresses the question (1.0 = fully "
        "responsive, 0.0 = off-topic).\n"
        "\n"
        f"Question: {case.question}\n"
        "\n"
        f"Expected answer (reference, may be partial): {expected}\n"
        "\n"
        "Produced answer:\n"
        f"{observed.answer}\n"
        "\n"
        "Cited evidence:\n"
        f"{evidence_block}\n"
        "\n"
        "Return ONLY valid JSON with no markdown formatting:\n"
        '{"faithfulness": <float 0.0-1.0>, "relevance": <float 0.0-1.0>}'
    )


def _evidence_block(observed: Observed) -> str:
    """Render the cited evidence for the judge prompt.

    Uses retrieved-hit text when the observation is an enriched
    :class:`QueryTraceRecord`; otherwise falls back to the answer's citation
    identifiers/titles from a :class:`QueryResponse`.
    """
    hits = getattr(observed, "retrieved_hits", None)
    if hits:
        lines = [
            f"  {rank + 1}. document={hit.document_id} chunk={hit.chunk_id} "
            f"score={hit.score:.4f}\n     {hit.text}"
            for rank, hit in enumerate(hits)
        ]
        return "\n".join(lines)

    citations = getattr(observed, "citations", None) or []
    if citations:
        lines = [
            f"  - document={citation.document_id} chunk={citation.chunk_id}"
            + (f" title={citation.title}" if citation.title else "")
            for citation in citations
        ]
        return "\n".join(lines)

    return "  (no evidence cited)"


# ---------------------------------------------------------------------------
# Response parsing / shaping
# ---------------------------------------------------------------------------


def _parse_scores(raw: str, case_id: str) -> LLMJudgeScores:
    """Parse the model's JSON into bounded scores (R7.3, R7.8).

    Both scores are coerced to floats and clamped into ``[0.0, 1.0]``. If the
    output is unparseable or lacks numeric scores, an error indication is
    recorded instead so the caller retains the other methods' results (R7.8).
    """
    payload = _extract_json_object(raw)
    if payload is None:
        logger.warning(
            "LLM judge returned unparseable output for case %s; recording error",
            case_id,
        )
        return LLMJudgeScores(error="llm_judge_unparseable_output")

    faithfulness = _coerce_score(payload.get("faithfulness"))
    relevance = _coerce_score(payload.get("relevance"))
    if faithfulness is None and relevance is None:
        return LLMJudgeScores(error="llm_judge_no_scores")

    return LLMJudgeScores(faithfulness=faithfulness, relevance=relevance)


def _coerce_score(value: Any) -> float | None:
    """Coerce a raw score into a float clamped to ``[0.0, 1.0]`` (R7.3)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response, or ``None``.

    Handles markdown-fenced JSON and JSON embedded in prose, mirroring the
    tolerant parsing used elsewhere for structured model output.
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
