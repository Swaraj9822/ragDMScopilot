# Feature: rag-trust-and-observability, Property 24: Deterministic checks and CI status
"""Property-based test for the deterministic evaluation method (task 12.2).

Feature: rag-trust-and-observability.

**Property 24: Deterministic checks and CI status.**

**Validates: Requirements 7.1, 7.5, 7.6.**

*For any* ``BenchmarkCase`` and any observed answer (``QueryResponse``):

* R7.1 — :func:`run_deterministic_checks` emits exactly one ``pass``/``fail``
  check for citation presence, one per required fact
  (``required_answer_terms``), and one for evidence-status correctness, and
  each outcome matches an independent oracle of the documented rule.
* R7.5 — a CI run (``ci_run_passed``) fails iff at least one deterministic
  check across the results produced ``fail``.
* R7.6 — the CI pass/fail determination is independent of ``LLM_Judge`` scores:
  mutating or clearing every ``llm_judge`` never changes ``ci_run_passed``.

The oracle re-derives each check outcome directly from the case/response inputs
and is compared against the implementation for every generated combination.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from rag_system.evaluation import (
    CITATION_PRESENCE_CHECK,
    EVIDENCE_STATUS_CHECK,
    REQUIRED_FACT_CHECK_PREFIX,
    ci_run_passed,
    evaluate_benchmark_case,
    run_deterministic_checks,
)
from rag_system.models import (
    BenchmarkCase,
    BenchmarkResult,
    Citation,
    DeterministicCheck,
    LLMJudgeScores,
    QueryResponse,
)

# --- fixtures / pools ------------------------------------------------------

# Distinct words, none a substring of another, so answer membership is exactly
# determined by the set of words placed into the answer text.
_WORDS = ["alpha", "beta", "gamma", "delta", "epsilon"]
_CHUNKS = ["chunk-1", "chunk-2", "chunk-3", "chunk-4", "chunk-5"]
_STATUSES = ["grounded", "partially_grounded", "insufficient_evidence"]


# --- generators ------------------------------------------------------------


@st.composite
def _case(draw: st.DrawFn) -> BenchmarkCase:
    """A benchmark case with arbitrary (unique) deterministic expectations."""
    required_terms = draw(st.lists(st.sampled_from(_WORDS), unique=True, max_size=4))
    required_chunks = draw(st.lists(st.sampled_from(_CHUNKS), unique=True, max_size=4))
    min_citations = draw(st.integers(min_value=0, max_value=5))
    max_citations = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5)))
    expected_status = draw(st.one_of(st.none(), st.sampled_from(_STATUSES)))
    return BenchmarkCase(
        id="c",
        question="q",
        required_answer_terms=required_terms,
        required_citation_chunk_ids=required_chunks,
        min_citations=min_citations,
        max_citations=max_citations,
        expected_evidence_status=expected_status,
    )


@st.composite
def _response(draw: st.DrawFn) -> QueryResponse:
    """An observed answer with arbitrary terms present and citations."""
    present = draw(st.lists(st.sampled_from(_WORDS), unique=True, max_size=5))
    answer = " ".join(present) if present else "(no terms)"
    chunk_ids = draw(st.lists(st.sampled_from(_CHUNKS), unique=True, max_size=5))
    citations = [
        Citation(
            document_id="doc-1",
            chunk_id=chunk_id,
            page_start=1,
            page_end=1,
            title="report.pdf",
        )
        for chunk_id in chunk_ids
    ]
    status = draw(st.sampled_from(_STATUSES))
    return QueryResponse(
        answer=answer,
        citations=citations,
        evidence_status=status,
        trace_id="trace-1",
        confidence="high",
    )


_score = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))


@st.composite
def _llm_scores(draw: st.DrawFn) -> LLMJudgeScores:
    return LLMJudgeScores(
        faithfulness=draw(_score),
        relevance=draw(_score),
        error=draw(st.one_of(st.none(), st.text(max_size=16))),
    )


@st.composite
def _result(draw: st.DrawFn) -> BenchmarkResult:
    """A benchmark result with arbitrary check outcomes and optional LLM scores."""
    outcomes = draw(st.lists(st.sampled_from(["pass", "fail"]), max_size=5))
    checks = [
        DeterministicCheck(name=f"chk-{i}", outcome=outcome)
        for i, outcome in enumerate(outcomes)
    ]
    llm = draw(st.one_of(st.none(), _llm_scores()))
    return BenchmarkResult(case_id="c", deterministic_checks=checks, llm_judge=llm)


# --- oracle ----------------------------------------------------------------


def _expected_outcomes(case: BenchmarkCase, response: QueryResponse) -> dict[str, str]:
    """Independent model of the R7.1 deterministic check outcomes."""
    outcomes: dict[str, str] = {}

    citation_ids = {citation.chunk_id for citation in response.citations}
    count = len(response.citations)
    citation_ok = (
        count >= case.min_citations
        and (case.max_citations is None or count <= case.max_citations)
        and all(chunk_id in citation_ids for chunk_id in case.required_citation_chunk_ids)
    )
    outcomes[CITATION_PRESENCE_CHECK] = "pass" if citation_ok else "fail"

    answer_cf = response.answer.casefold()
    for term in case.required_answer_terms:
        outcomes[f"{REQUIRED_FACT_CHECK_PREFIX}{term}"] = (
            "pass" if term.casefold() in answer_cf else "fail"
        )

    status_ok = (
        case.expected_evidence_status is None
        or response.evidence_status == case.expected_evidence_status
    )
    outcomes[EVIDENCE_STATUS_CHECK] = "pass" if status_ok else "fail"

    return outcomes


# --- properties ------------------------------------------------------------


@given(case=_case(), response=_response())
def test_deterministic_checks_match_oracle(
    case: BenchmarkCase, response: QueryResponse
) -> None:
    """R7.1: each deterministic check yields pass/fail matching the rule."""
    checks = run_deterministic_checks(case, response)
    actual = {check.name: check.outcome for check in checks}
    expected = _expected_outcomes(case, response)

    # Exactly the expected checks are emitted (citation presence, one per
    # required fact, and evidence-status correctness), with no duplicates.
    assert len(checks) == len(expected)
    assert set(actual) == set(expected)
    # Every outcome is a binary pass/fail.
    assert all(outcome in {"pass", "fail"} for outcome in actual.values())
    # Each check's outcome matches the independent oracle.
    assert actual == expected


@given(results=st.lists(_result(), max_size=6))
def test_ci_fails_iff_any_deterministic_check_fails(
    results: list[BenchmarkResult],
) -> None:
    """R7.5: a CI run fails iff any deterministic check failed."""
    any_fail = any(
        check.outcome == "fail"
        for result in results
        for check in result.deterministic_checks
    )
    assert ci_run_passed(results) == (not any_fail)


@given(results=st.lists(_result(), max_size=6))
def test_ci_status_independent_of_llm_scores(
    results: list[BenchmarkResult],
) -> None:
    """R7.6: CI pass/fail never depends on LLM_Judge scores."""
    baseline = ci_run_passed(results)

    # Worst-possible judge scores plus a timeout error must not change CI.
    for result in results:
        result.llm_judge = LLMJudgeScores(faithfulness=0.0, relevance=0.0, error="timeout")
    assert ci_run_passed(results) == baseline

    # Clearing judge scores entirely must not change CI either.
    for result in results:
        result.llm_judge = None
    assert ci_run_passed(results) == baseline


@given(pairs=st.lists(st.tuples(_case(), _response()), max_size=5))
def test_ci_status_over_evaluated_cases(
    pairs: list[tuple[BenchmarkCase, QueryResponse]],
) -> None:
    """R7.1 + R7.5: CI over evaluated cases fails iff any check failed."""
    results = [evaluate_benchmark_case(case, response) for case, response in pairs]

    expected_all_pass = all(
        outcome == "pass"
        for case, response in pairs
        for outcome in _expected_outcomes(case, response).values()
    )
    assert ci_run_passed(results) == expected_all_pass
