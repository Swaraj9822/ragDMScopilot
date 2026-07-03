from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rag_system.models import QueryRequest, QueryResponse

if TYPE_CHECKING:
    from rag_system.models import (
        BenchmarkCase,
        BenchmarkResult,
        DeterministicCheck,
        QueryTraceRecord,
    )


class GoldenCase(BaseModel):
    id: str
    question: str
    document_ids: list[str] | None = None
    expected_evidence_status: str | None = None
    required_answer_terms: list[str] = Field(default_factory=list)
    forbidden_answer_terms: list[str] = Field(default_factory=list)
    required_citation_chunk_ids: list[str] = Field(default_factory=list)
    min_citations: int = 0
    max_citations: int | None = None
    min_confidence: str | None = None
    required_insufficient_reason_terms: list[str] = Field(default_factory=list)


class GoldenCaseResult(BaseModel):
    id: str
    passed: bool
    failures: list[str] = Field(default_factory=list)


class GoldenEvaluationSummary(BaseModel):
    total: int
    passed: int
    failed: int
    results: list[GoldenCaseResult]


Runner = Callable[[QueryRequest], QueryResponse]

_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def load_golden_cases(path: str | Path) -> list[GoldenCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("Golden test file must be a list or an object with a 'cases' list.")
    return [GoldenCase.model_validate(case) for case in raw_cases]


def evaluate_cases(cases: Iterable[GoldenCase], runner: Runner) -> GoldenEvaluationSummary:
    results = []
    for case in cases:
        response = runner(QueryRequest(question=case.question, document_ids=case.document_ids))
        results.append(evaluate_response(case, response))

    passed = sum(1 for result in results if result.passed)
    return GoldenEvaluationSummary(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )


def evaluate_response(case: GoldenCase, response: QueryResponse) -> GoldenCaseResult:
    failures: list[str] = []
    answer = response.answer.casefold()
    citation_ids = {citation.chunk_id for citation in response.citations}

    if (
        case.expected_evidence_status is not None
        and response.evidence_status != case.expected_evidence_status
    ):
        failures.append(
            "expected evidence_status "
            f"{case.expected_evidence_status!r}, got {response.evidence_status!r}"
        )

    for term in case.required_answer_terms:
        if term.casefold() not in answer:
            failures.append(f"answer is missing required term {term!r}")

    for term in case.forbidden_answer_terms:
        if term.casefold() in answer:
            failures.append(f"answer contains forbidden term {term!r}")

    for chunk_id in case.required_citation_chunk_ids:
        if chunk_id not in citation_ids:
            failures.append(f"missing required citation chunk_id {chunk_id!r}")

    citation_count = len(response.citations)
    if citation_count < case.min_citations:
        failures.append(f"expected at least {case.min_citations} citation(s), got {citation_count}")
    if case.max_citations is not None and citation_count > case.max_citations:
        failures.append(f"expected at most {case.max_citations} citation(s), got {citation_count}")

    if case.min_confidence is not None:
        _check_min_confidence(case.min_confidence, response.confidence, failures)

    reason = (response.insufficient_evidence_reason or "").casefold()
    for term in case.required_insufficient_reason_terms:
        if term.casefold() not in reason:
            failures.append(f"insufficient evidence reason is missing required term {term!r}")

    return GoldenCaseResult(id=case.id, passed=not failures, failures=failures)


def _check_min_confidence(
    expected_minimum: str,
    actual: str | None,
    failures: list[str],
) -> None:
    expected_rank = _CONFIDENCE_RANK.get(expected_minimum.casefold())
    actual_rank = _CONFIDENCE_RANK.get((actual or "").casefold())
    if expected_rank is None:
        failures.append(f"unknown minimum confidence {expected_minimum!r}")
        return
    if actual_rank is None:
        failures.append(f"expected confidence at least {expected_minimum!r}, got {actual!r}")
        return
    if actual_rank < expected_rank:
        failures.append(f"expected confidence at least {expected_minimum!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# Multi-method evaluation — deterministic method (R7.1, R7.4, R7.5, R7.6)
#
# The existing ``GoldenCase`` / ``evaluate_response`` logic above expresses the
# deterministic checks as a flat list of human-readable failure strings. The
# functions below re-express the same checks as structured, per-check
# ``pass``/``fail`` outcomes (``DeterministicCheck``) so that a continuous
# integration run can fail iff *any* deterministic check fails, independent of
# any LLM_Judge scores (R7.5, R7.6). Retrieval metrics (task 12.3) and the LLM
# judge (task 12.5) are layered on top of these results in their own modules.
# ---------------------------------------------------------------------------


#: Name of the always-present citation-presence deterministic check (R7.1).
CITATION_PRESENCE_CHECK = "citation_presence"
#: Name of the always-present evidence-status-correctness deterministic check.
EVIDENCE_STATUS_CHECK = "evidence_status"
#: Prefix for the per-required-fact deterministic checks (one per term).
REQUIRED_FACT_CHECK_PREFIX = "required_fact:"


class EvaluationSetValidationError(ValueError):
    """Raised when an ``Evaluation_Set`` fails validation (e.g. no human-reviewed case)."""


def run_deterministic_checks(
    case: "BenchmarkCase",
    observed: "QueryResponse | QueryTraceRecord",
) -> list["DeterministicCheck"]:
    """Produce per-check ``pass``/``fail`` outcomes for a benchmark case (R7.1).

    Exactly the three deterministic-check categories called out by R7.1 are
    emitted:

    * ``citation_presence`` — a single check covering the case's citation
      expectations (minimum/maximum count and any required citation chunk ids).
      Always emitted; it passes vacuously when the case states no citation
      expectation.
    * ``required_fact:{term}`` — one check per required fact
      (``required_answer_terms``); passes when the term appears in the answer.
    * ``evidence_status`` — a single check for Evidence_Status correctness.
      Always emitted; it passes vacuously when the case states no expected
      status.

    ``observed`` may be a :class:`~rag_system.models.QueryResponse` produced by
    a runner or an enriched :class:`~rag_system.models.QueryTraceRecord`
    (task 1.7) read back for per-query context — both expose ``answer``,
    ``citations`` and ``evidence_status``.
    """
    from rag_system.models import DeterministicCheck

    checks: list[DeterministicCheck] = []

    # 1. Citation presence.
    citation_ids = {citation.chunk_id for citation in observed.citations}
    citation_count = len(observed.citations)
    citation_ok = citation_count >= case.min_citations
    if case.max_citations is not None:
        citation_ok = citation_ok and citation_count <= case.max_citations
    citation_ok = citation_ok and all(
        chunk_id in citation_ids for chunk_id in case.required_citation_chunk_ids
    )
    checks.append(
        DeterministicCheck(
            name=CITATION_PRESENCE_CHECK,
            outcome="pass" if citation_ok else "fail",
        )
    )

    # 2. Presence of each required fact.
    answer = observed.answer.casefold()
    for term in case.required_answer_terms:
        present = term.casefold() in answer
        checks.append(
            DeterministicCheck(
                name=f"{REQUIRED_FACT_CHECK_PREFIX}{term}",
                outcome="pass" if present else "fail",
            )
        )

    # 3. Evidence-status correctness.
    if case.expected_evidence_status is None:
        status_ok = True
    else:
        status_ok = observed.evidence_status == case.expected_evidence_status
    checks.append(
        DeterministicCheck(
            name=EVIDENCE_STATUS_CHECK,
            outcome="pass" if status_ok else "fail",
        )
    )

    return checks


def evaluate_benchmark_case(
    case: "BenchmarkCase",
    observed: "QueryResponse | QueryTraceRecord",
) -> "BenchmarkResult":
    """Evaluate a single benchmark case's deterministic checks (R7.1, R7.7).

    Retrieval metrics (task 12.3) and LLM judge scores (task 12.5) are attached
    to the returned :class:`~rag_system.models.BenchmarkResult` by their own
    modules; here only the deterministic method is populated.
    """
    from rag_system.models import BenchmarkResult

    return BenchmarkResult(
        case_id=case.id,
        deterministic_checks=run_deterministic_checks(case, observed),
    )


def validate_evaluation_set(cases: Iterable["BenchmarkCase"]) -> None:
    """Validate an ``Evaluation_Set`` before a run (R7.4).

    The set must contain at least one human-reviewed benchmark case. Raises
    :class:`EvaluationSetValidationError` otherwise.
    """
    if not any(case.human_reviewed for case in cases):
        raise EvaluationSetValidationError(
            "Evaluation set must include at least one human-reviewed benchmark case."
        )


def evaluate_benchmark_cases(
    cases: Iterable["BenchmarkCase"],
    runner: Callable[[QueryRequest], "QueryResponse | QueryTraceRecord"],
    *,
    validate_set: bool = True,
) -> list["BenchmarkResult"]:
    """Run the deterministic method across an evaluation set (R7.1, R7.4, R7.7).

    When ``validate_set`` is true the set is validated up front (R7.4). The
    ``runner`` produces the observed answer for each case — either a
    :class:`~rag_system.models.QueryResponse` or an enriched
    :class:`~rag_system.models.QueryTraceRecord`.
    """
    cases = list(cases)
    if validate_set:
        validate_evaluation_set(cases)

    results: list[BenchmarkResult] = []
    for case in cases:
        observed = runner(QueryRequest(question=case.question, document_ids=case.document_ids))
        results.append(evaluate_benchmark_case(case, observed))
    return results


def ci_run_passed(results: Iterable["BenchmarkResult"]) -> bool:
    """Report the CI pass/fail status for a set of results (R7.5, R7.6).

    The run passes iff *every* deterministic check across all results passed;
    equivalently it fails iff at least one deterministic check produced
    ``fail``. LLM_Judge scores are deliberately ignored so they can never
    influence CI status (R7.6).
    """
    return all(
        check.outcome == "pass"
        for result in results
        for check in result.deterministic_checks
    )
