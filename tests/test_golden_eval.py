from pathlib import Path

from rag_system.evaluation import GoldenCase, evaluate_cases, evaluate_response, load_golden_cases
from rag_system.models import Citation, QueryRequest, QueryResponse


GOLDEN_FILE = Path(__file__).parent / "golden" / "rag_golden_set.json"


def test_starter_golden_set_passes_against_deterministic_runner() -> None:
    cases = load_golden_cases(GOLDEN_FILE)

    summary = evaluate_cases(cases, _golden_runner)

    assert summary.total == 2
    assert summary.failed == 0
    assert [result.id for result in summary.results] == [
        "revenue-grounded",
        "margin-insufficient-evidence",
    ]


def test_golden_evaluator_reports_actionable_failures() -> None:
    case = GoldenCase(
        id="missing-citation",
        question="What was revenue?",
        expected_evidence_status="grounded",
        required_answer_terms=["revenue", "10"],
        required_citation_chunk_ids=["chunk-expected"],
        min_citations=1,
        min_confidence="high",
    )
    response = QueryResponse(
        answer="Revenue was 10.",
        citations=[],
        evidence_status="grounded",
        trace_id="trace-1",
        confidence="medium",
    )

    result = evaluate_response(case, response)

    assert not result.passed
    assert any("missing required citation" in failure for failure in result.failures)
    assert any("at least 1 citation" in failure for failure in result.failures)
    assert any("confidence at least 'high'" in failure for failure in result.failures)


def _golden_runner(request: QueryRequest) -> QueryResponse:
    if "revenue" in request.question.lower():
        return QueryResponse(
            answer="Revenue was 10.",
            citations=[
                Citation(
                    document_id="doc-revenue",
                    chunk_id="doc-revenue:chunk-1",
                    page_start=2,
                    page_end=2,
                    title="report.pdf",
                )
            ],
            evidence_status="grounded",
            trace_id="trace-revenue",
            confidence="high",
        )

    if "margin" in request.question.lower():
        return QueryResponse(
            answer="The available documents do not contain enough evidence to answer margin.",
            citations=[],
            evidence_status="insufficient_evidence",
            trace_id="trace-margin",
            confidence="low",
            insufficient_evidence_reason="The retrieved revenue document does not mention margin.",
        )

    raise AssertionError(f"Unexpected golden question: {request.question}")
