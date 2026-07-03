"""Tests for the LLM-as-judge evaluation method (R7.3, R7.6, R7.7, R7.8, task 12.5).

Feature: rag-trust-and-observability.

The judge is LLM-based; here the model is stubbed so the score shaping, the
distinct-model/timeout wiring, the scheduling cadence, and the R7.8 timeout path
are verified deterministically without live calls. These tests cover:

* R7.3 — faithfulness/relevance scores are within ``[0.0, 1.0]`` when enabled,
  and out-of-range model output is clamped into range.
* R7.6 — the judge runs on a scheduled interval, and its scores never influence
  CI pass/fail (deterministic checks alone decide CI status).
* R7.7 — evaluation results record deterministic outcomes, retrieval metrics,
  and LLM scores per case, and round-trip through serialization.
* R7.8 — a per-case timeout (or model error / unparseable output) records an
  error indication in ``LLMJudgeScores.error`` while the case's deterministic
  and retrieval results are retained; the judge uses its own shorter read
  timeout and a bounded thinking budget.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from rag_system.config import Settings
from rag_system.evaluation import ci_run_passed
from rag_system.llm_judge import (
    JudgeTimeoutError,
    LLMJudge,
    judge_run_due,
    next_judge_run_at,
)
from rag_system.models import (
    BenchmarkCase,
    BenchmarkResult,
    Citation,
    DeterministicCheck,
    LLMJudgeScores,
    QueryResponse,
    RetrievalMetrics,
)

_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


class _StubLLM:
    """Records judge prompts and returns a canned model response.

    ``response`` may be a string (returned verbatim), an exception (raised to
    simulate a model error), or a callable (invoked to produce the response,
    e.g. to sleep past the per-case timeout).
    """

    model_id = "gemini-3.1-pro"
    provider = "gemini"

    def __init__(self, response: str | Exception | Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        self.calls.append(
            {
                "prompt": prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking_budget": thinking_budget,
            }
        )
        if isinstance(self._response, Exception):
            raise self._response
        if callable(self._response):
            return self._response(), {}
        return self._response, {}

    def generate_stream(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused
        raise NotImplementedError


def _case(**overrides: Any) -> BenchmarkCase:
    base: dict[str, Any] = dict(
        id="case-1",
        question="What is the refund window?",
        expected_answer="Refunds are allowed within 30 days.",
        human_reviewed=True,
    )
    base.update(overrides)
    return BenchmarkCase(**base)


def _response(**overrides: Any) -> QueryResponse:
    base: dict[str, Any] = dict(
        answer="Refunds are allowed within 30 days of purchase.",
        citations=[Citation(document_id="doc-1", chunk_id="c1", title="Refund Policy")],
        evidence_status="supported",
        trace_id="trace-1",
    )
    base.update(overrides)
    return QueryResponse(**base)


def _scores_json(faithfulness: float = 0.9, relevance: float = 0.8) -> str:
    return json.dumps({"faithfulness": faithfulness, "relevance": relevance})


# ---------------------------------------------------------------------------
# R7.3 — bounded scores when enabled
# ---------------------------------------------------------------------------


def test_score_case_produces_bounded_scores() -> None:
    judge = LLMJudge(_settings(), llm=_StubLLM(_scores_json(0.9, 0.75)))

    scores = judge.score_case(_case(), _response())

    assert scores.error is None
    assert scores.faithfulness == pytest.approx(0.9)
    assert scores.relevance == pytest.approx(0.75)
    assert 0.0 <= scores.faithfulness <= 1.0
    assert 0.0 <= scores.relevance <= 1.0


def test_out_of_range_scores_are_clamped() -> None:
    judge = LLMJudge(_settings(), llm=_StubLLM(_scores_json(1.7, -0.4)))

    scores = judge.score_case(_case(), _response())

    assert scores.error is None
    assert scores.faithfulness == 1.0
    assert scores.relevance == 0.0


def test_score_case_handles_markdown_fenced_json() -> None:
    raw = f"```json\n{_scores_json(0.6, 0.6)}\n```"
    judge = LLMJudge(_settings(), llm=_StubLLM(raw))

    scores = judge.score_case(_case(), _response())

    assert scores.error is None
    assert scores.faithfulness == pytest.approx(0.6)


def test_score_case_uses_bounded_thinking_budget() -> None:
    llm = _StubLLM(_scores_json())
    judge = LLMJudge(_settings(RAG_LLM_JUDGE_THINKING_BUDGET=2048), llm=llm)

    judge.score_case(_case(), _response())

    assert llm.calls[0]["thinking_budget"] == 2048
    assert llm.calls[0]["temperature"] == 0.0


def test_prompt_includes_question_answer_and_evidence() -> None:
    llm = _StubLLM(_scores_json())
    judge = LLMJudge(_settings(), llm=llm)

    judge.score_case(_case(), _response())

    prompt = llm.calls[0]["prompt"]
    assert "What is the refund window?" in prompt
    assert "faithfulness" in prompt
    assert "relevance" in prompt
    assert "Cited evidence" in prompt


# ---------------------------------------------------------------------------
# R7.8 — per-case timeout / errors record an error indication
# ---------------------------------------------------------------------------


def test_injected_timeout_records_error() -> None:
    def _always_timeout(_fn, _timeout):
        raise JudgeTimeoutError

    judge = LLMJudge(
        _settings(), llm=_StubLLM(_scores_json()), timeout_runner=_always_timeout
    )

    scores = judge.score_case(_case(), _response())

    assert scores.faithfulness is None
    assert scores.relevance is None
    assert scores.error == "llm_judge_timeout"


def test_real_timeout_runner_trips_on_slow_call() -> None:
    # A stub that sleeps past a tiny per-case timeout exercises the real
    # thread-based per-case timeout wrapper (R7.8) without waiting 60s.
    def _slow() -> str:
        time.sleep(0.5)
        return _scores_json()

    judge = LLMJudge(
        _settings(), llm=_StubLLM(_slow), per_case_timeout_s=0.05
    )

    scores = judge.score_case(_case(), _response())

    assert scores.error == "llm_judge_timeout"
    assert scores.faithfulness is None


def test_model_error_records_error_indication() -> None:
    judge = LLMJudge(_settings(), llm=_StubLLM(RuntimeError("boom")))

    scores = judge.score_case(_case(), _response())

    assert scores.faithfulness is None
    assert scores.error is not None
    assert "boom" in scores.error


def test_unparseable_output_records_error_indication() -> None:
    judge = LLMJudge(_settings(), llm=_StubLLM("not json at all"))

    scores = judge.score_case(_case(), _response())

    assert scores.faithfulness is None
    assert scores.error == "llm_judge_unparseable_output"


def test_timeout_retains_deterministic_and_retrieval_results() -> None:
    # R7.8: on a per-case timeout the deterministic + retrieval results survive;
    # only the LLM scores become an error indication.
    def _always_timeout(_fn, _timeout):
        raise JudgeTimeoutError

    case = _case()
    deterministic = [DeterministicCheck(name="citation_presence", outcome="pass")]
    retrieval = RetrievalMetrics(
        recall_at_k=0.5, precision_at_k=0.5, mrr_at_k=1.0, depth=10
    )
    result = BenchmarkResult(
        case_id=case.id,
        deterministic_checks=deterministic,
        retrieval_metrics=retrieval,
    )
    judge = LLMJudge(
        _settings(), llm=_StubLLM(_scores_json()), timeout_runner=_always_timeout
    )

    scored = judge.score_results(
        [case], [result], {case.id: _response()}, enabled=True
    )

    assert len(scored) == 1
    assert scored[0].deterministic_checks == deterministic
    assert scored[0].retrieval_metrics == retrieval
    assert scored[0].llm_judge is not None
    assert scored[0].llm_judge.error == "llm_judge_timeout"


# ---------------------------------------------------------------------------
# R7.3 / R7.7 — score_results gating and per-case recording
# ---------------------------------------------------------------------------


def test_score_results_disabled_leaves_scores_none() -> None:
    case = _case()
    result = BenchmarkResult(case_id=case.id)
    judge = LLMJudge(_settings(), llm=_StubLLM(_scores_json()))

    scored = judge.score_results(
        [case], [result], {case.id: _response()}, enabled=False
    )

    assert scored[0].llm_judge is None


def test_score_results_enabled_attaches_scores() -> None:
    case = _case()
    result = BenchmarkResult(
        case_id=case.id,
        deterministic_checks=[DeterministicCheck(name="citation_presence", outcome="pass")],
    )
    judge = LLMJudge(_settings(), llm=_StubLLM(_scores_json(0.9, 0.8)))

    scored = judge.score_results(
        [case], [result], {case.id: _response()}, enabled=True
    )

    assert scored[0].llm_judge is not None
    assert scored[0].llm_judge.faithfulness == pytest.approx(0.9)
    assert scored[0].llm_judge.relevance == pytest.approx(0.8)
    # deterministic checks are retained.
    assert scored[0].deterministic_checks[0].outcome == "pass"


def test_score_results_missing_observation_records_error() -> None:
    case = _case()
    result = BenchmarkResult(case_id=case.id)
    judge = LLMJudge(_settings(), llm=_StubLLM(_scores_json()))

    scored = judge.score_results([case], [result], {}, enabled=True)

    assert scored[0].llm_judge is not None
    assert scored[0].llm_judge.error == "llm_judge_missing_observation"


def test_benchmark_result_with_scores_round_trips() -> None:
    # R7.7: results record deterministic outcomes, retrieval metrics, and LLM
    # scores per case; serializing then deserializing preserves every field.
    result = BenchmarkResult(
        case_id="case-1",
        deterministic_checks=[DeterministicCheck(name="citation_presence", outcome="fail")],
        retrieval_metrics=RetrievalMetrics(
            recall_at_k=0.4, precision_at_k=0.6, mrr_at_k=0.5, depth=10
        ),
        llm_judge=LLMJudgeScores(faithfulness=0.7, relevance=0.65),
    )

    restored = BenchmarkResult.model_validate(json.loads(result.model_dump_json()))

    assert restored == result


# ---------------------------------------------------------------------------
# R7.6 — LLM scores excluded from CI pass/fail
# ---------------------------------------------------------------------------


def test_llm_scores_do_not_affect_ci_status() -> None:
    # A run whose deterministic checks all pass is a CI pass regardless of low
    # LLM judge scores (R7.6).
    passing = BenchmarkResult(
        case_id="case-1",
        deterministic_checks=[DeterministicCheck(name="citation_presence", outcome="pass")],
        llm_judge=LLMJudgeScores(faithfulness=0.0, relevance=0.0),
    )
    assert ci_run_passed([passing]) is True

    # A deterministic fail is a CI fail even with perfect LLM judge scores.
    failing = BenchmarkResult(
        case_id="case-2",
        deterministic_checks=[DeterministicCheck(name="citation_presence", outcome="fail")],
        llm_judge=LLMJudgeScores(faithfulness=1.0, relevance=1.0),
    )
    assert ci_run_passed([failing]) is False


# ---------------------------------------------------------------------------
# R7.6 — scheduling cadence
# ---------------------------------------------------------------------------


def test_judge_run_due_when_never_run() -> None:
    assert judge_run_due(None, datetime.now(timezone.utc), 24) is True


def test_judge_run_due_after_interval_elapsed() -> None:
    now = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=24)
    assert judge_run_due(last, now, 24) is True


def test_judge_run_not_due_before_interval() -> None:
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=1)
    assert judge_run_due(last, now, 24) is False


def test_next_judge_run_at_is_one_interval_after_last() -> None:
    last = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert next_judge_run_at(last, 24) == last + timedelta(hours=24)
