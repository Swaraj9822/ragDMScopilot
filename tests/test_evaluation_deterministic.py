"""Unit tests for the deterministic evaluation method (R7.1, R7.4, R7.5, R7.6).

Feature: rag-trust-and-observability (task 12.1 — deterministic checks).

These exercise the structured per-check ``pass``/``fail`` outcomes produced by
``run_deterministic_checks`` (citation presence, each required fact, and
evidence-status correctness), the CI pass/fail determination that ignores
LLM_Judge scores, and evaluation-set validation requiring a human-reviewed case.
"""

import pytest

from rag_system.evaluation import (
    CITATION_PRESENCE_CHECK,
    EVIDENCE_STATUS_CHECK,
    REQUIRED_FACT_CHECK_PREFIX,
    EvaluationSetValidationError,
    ci_run_passed,
    evaluate_benchmark_case,
    evaluate_benchmark_cases,
    run_deterministic_checks,
    validate_evaluation_set,
)
from rag_system.models import (
    BenchmarkCase,
    Citation,
    LLMJudgeScores,
    QueryRequest,
    QueryResponse,
    QueryTraceRecord,
)


def _citation(chunk_id: str = "doc-1:chunk-1") -> Citation:
    return Citation(
        document_id="doc-1",
        chunk_id=chunk_id,
        page_start=1,
        page_end=1,
        title="report.pdf",
    )


def _response(
    *,
    answer: str = "Revenue was 10 and margin was 5.",
    citations: list[Citation] | None = None,
    evidence_status: str = "grounded",
) -> QueryResponse:
    return QueryResponse(
        answer=answer,
        citations=[_citation()] if citations is None else citations,
        evidence_status=evidence_status,
        trace_id="trace-1",
        confidence="high",
    )


def _outcomes(checks) -> dict[str, str]:
    return {check.name: check.outcome for check in checks}


# --------------------------------------------------------------------------
# run_deterministic_checks — the three check categories (R7.1)
# --------------------------------------------------------------------------


def test_all_checks_pass_when_expectations_met():
    case = BenchmarkCase(
        id="c1",
        question="What was revenue and margin?",
        expected_evidence_status="grounded",
        required_answer_terms=["revenue", "margin"],
        required_citation_chunk_ids=["doc-1:chunk-1"],
        min_citations=1,
    )

    checks = run_deterministic_checks(case, _response())
    outcomes = _outcomes(checks)

    assert outcomes[CITATION_PRESENCE_CHECK] == "pass"
    assert outcomes[f"{REQUIRED_FACT_CHECK_PREFIX}revenue"] == "pass"
    assert outcomes[f"{REQUIRED_FACT_CHECK_PREFIX}margin"] == "pass"
    assert outcomes[EVIDENCE_STATUS_CHECK] == "pass"
    # One check per required fact plus citation-presence and evidence-status.
    assert len(checks) == 4


def test_every_check_has_binary_outcome():
    case = BenchmarkCase(
        id="c1",
        question="q",
        expected_evidence_status="grounded",
        required_answer_terms=["missing-term"],
        min_citations=5,
    )

    checks = run_deterministic_checks(case, _response(evidence_status="insufficient_evidence"))

    assert all(check.outcome in {"pass", "fail"} for check in checks)


def test_citation_presence_fails_below_min_citations():
    case = BenchmarkCase(id="c1", question="q", min_citations=2)

    checks = run_deterministic_checks(case, _response(citations=[_citation()]))

    assert _outcomes(checks)[CITATION_PRESENCE_CHECK] == "fail"


def test_citation_presence_fails_when_required_chunk_missing():
    case = BenchmarkCase(
        id="c1",
        question="q",
        required_citation_chunk_ids=["doc-1:chunk-expected"],
        min_citations=1,
    )

    checks = run_deterministic_checks(case, _response())

    assert _outcomes(checks)[CITATION_PRESENCE_CHECK] == "fail"


def test_citation_presence_fails_above_max_citations():
    case = BenchmarkCase(id="c1", question="q", max_citations=1)
    citations = [_citation("doc-1:chunk-1"), _citation("doc-1:chunk-2")]

    checks = run_deterministic_checks(case, _response(citations=citations))

    assert _outcomes(checks)[CITATION_PRESENCE_CHECK] == "fail"


def test_required_fact_fails_when_term_absent():
    case = BenchmarkCase(id="c1", question="q", required_answer_terms=["ebitda"])

    checks = run_deterministic_checks(case, _response(answer="Revenue was 10."))

    assert _outcomes(checks)[f"{REQUIRED_FACT_CHECK_PREFIX}ebitda"] == "fail"


def test_required_fact_matching_is_case_insensitive():
    case = BenchmarkCase(id="c1", question="q", required_answer_terms=["REVENUE"])

    checks = run_deterministic_checks(case, _response(answer="revenue was 10"))

    assert _outcomes(checks)[f"{REQUIRED_FACT_CHECK_PREFIX}REVENUE"] == "pass"


def test_evidence_status_fails_on_mismatch():
    case = BenchmarkCase(id="c1", question="q", expected_evidence_status="grounded")

    checks = run_deterministic_checks(case, _response(evidence_status="insufficient_evidence"))

    assert _outcomes(checks)[EVIDENCE_STATUS_CHECK] == "fail"


def test_vacuous_checks_pass_when_no_expectations():
    case = BenchmarkCase(id="c1", question="q")

    checks = run_deterministic_checks(case, _response(citations=[]))
    outcomes = _outcomes(checks)

    # Citation-presence and evidence-status are always emitted and pass vacuously.
    assert outcomes[CITATION_PRESENCE_CHECK] == "pass"
    assert outcomes[EVIDENCE_STATUS_CHECK] == "pass"
    assert len(checks) == 2


def test_checks_read_enriched_query_trace_record():
    case = BenchmarkCase(
        id="c1",
        question="q",
        expected_evidence_status="grounded",
        required_answer_terms=["revenue"],
        min_citations=1,
    )
    trace = QueryTraceRecord(
        trace_id="trace-1",
        question="q",
        route="rag",
        answer="Revenue was 10.",
        evidence_status="grounded",
        citations=[_citation()],
        latency_ms=42.0,
    )

    checks = run_deterministic_checks(case, trace)
    outcomes = _outcomes(checks)

    assert outcomes[CITATION_PRESENCE_CHECK] == "pass"
    assert outcomes[f"{REQUIRED_FACT_CHECK_PREFIX}revenue"] == "pass"
    assert outcomes[EVIDENCE_STATUS_CHECK] == "pass"


# --------------------------------------------------------------------------
# evaluate_benchmark_case / evaluate_benchmark_cases
# --------------------------------------------------------------------------


def test_evaluate_benchmark_case_populates_only_deterministic_checks():
    case = BenchmarkCase(id="c1", question="q", required_answer_terms=["revenue"])

    result = evaluate_benchmark_case(case, _response())

    assert result.case_id == "c1"
    assert result.deterministic_checks
    assert result.retrieval_metrics is None
    assert result.llm_judge is None


def test_evaluate_benchmark_cases_runs_runner_and_validates_set():
    cases = [
        BenchmarkCase(
            id="c1",
            question="What was revenue?",
            required_answer_terms=["revenue"],
            human_reviewed=True,
        )
    ]

    def runner(request: QueryRequest) -> QueryResponse:
        assert request.question == "What was revenue?"
        return _response(answer="Revenue was 10.")

    results = evaluate_benchmark_cases(cases, runner)

    assert len(results) == 1
    assert ci_run_passed(results)


def test_evaluate_benchmark_cases_rejects_set_without_human_review():
    cases = [BenchmarkCase(id="c1", question="q")]

    with pytest.raises(EvaluationSetValidationError):
        evaluate_benchmark_cases(cases, lambda _req: _response())


# --------------------------------------------------------------------------
# CI status (R7.5, R7.6)
# --------------------------------------------------------------------------


def test_ci_passes_when_all_checks_pass():
    case = BenchmarkCase(id="c1", question="q", required_answer_terms=["revenue"])
    result = evaluate_benchmark_case(case, _response(answer="Revenue was 10."))

    assert ci_run_passed([result]) is True


def test_ci_fails_when_any_check_fails():
    ok = evaluate_benchmark_case(
        BenchmarkCase(id="c1", question="q", required_answer_terms=["revenue"]),
        _response(answer="Revenue was 10."),
    )
    bad = evaluate_benchmark_case(
        BenchmarkCase(id="c2", question="q", required_answer_terms=["ebitda"]),
        _response(answer="Revenue was 10."),
    )

    assert ci_run_passed([ok, bad]) is False


def test_ci_status_ignores_llm_judge_scores():
    case = BenchmarkCase(id="c1", question="q", required_answer_terms=["revenue"])
    result = evaluate_benchmark_case(case, _response(answer="Revenue was 10."))
    # A poor LLM judge score must not influence CI pass/fail (R7.6).
    result.llm_judge = LLMJudgeScores(faithfulness=0.0, relevance=0.0)

    assert ci_run_passed([result]) is True


def test_ci_passes_on_empty_results():
    assert ci_run_passed([]) is True


# --------------------------------------------------------------------------
# validate_evaluation_set (R7.4)
# --------------------------------------------------------------------------


def test_validate_evaluation_set_requires_human_reviewed_case():
    cases = [
        BenchmarkCase(id="c1", question="q"),
        BenchmarkCase(id="c2", question="q"),
    ]

    with pytest.raises(EvaluationSetValidationError):
        validate_evaluation_set(cases)


def test_validate_evaluation_set_passes_with_one_human_reviewed_case():
    cases = [
        BenchmarkCase(id="c1", question="q"),
        BenchmarkCase(id="c2", question="q", human_reviewed=True),
    ]

    # Should not raise.
    validate_evaluation_set(cases)
