import json
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from rag_system.models import QueryRequest, QueryResponse


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
