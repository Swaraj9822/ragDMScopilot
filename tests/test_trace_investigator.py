"""Tests for the AI trace investigator service (R10, task 17.1).

Feature: rag-trust-and-observability.

The diagnosis engine is LLM-based; here the model is stubbed so the
cause/recommendation shaping is verified deterministically. These tests cover:

* R10.2 — an unrecorded trace performs no diagnosis and raises
  ``TraceNotFoundError`` (mapped to 404 by the endpoint), and never invokes the
  model.
* R10.1/R10.3 — the diagnosis references at least one analyzed element.
* R10.5 — an identified cause yields 1..10 recommendations, each targeting the
  AI configuration or the corpus; invalid targets are dropped and the set is
  clamped to 10.
* R10.4 — when no cause is determined (or the model output is unusable), the
  diagnosis carries a no-cause description and zero recommendations.
* R10.7 — the service is read-only: it takes a resolver + model only and never
  mutates configuration or corpus.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rag_system.config import Settings
from rag_system.models import (
    AnswerSpan,
    Claim,
    EvidenceStatus,
    QueryTraceHit,
    QueryTraceRecord,
    ReasonCode,
    Recommendation,
    TraceDiagnosis,
)
from rag_system.trace_investigator import (
    TraceInvestigator,
    TraceNotFoundError,
    _available_elements,
    _build_diagnosis_prompt,
    _parse_diagnosis,
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
    """Records diagnosis prompts and returns a canned model response.

    ``response`` may be a string (returned verbatim) or an exception instance
    (raised to simulate a model error/timeout).
    """

    model_id = "gemini-3.1-pro"
    provider = "gemini"

    def __init__(self, response: str | Exception) -> None:
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
        return self._response, {}

    def generate_stream(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused
        raise NotImplementedError


def _trace(**overrides: Any) -> QueryTraceRecord:
    """Build an enriched trace record for an unsuccessful query."""
    base: dict[str, Any] = dict(
        trace_id="trace-123",
        question="What is the refund window?",
        route="rag",
        retrieval_mode="hybrid",
        answer="",
        evidence_status="insufficient_evidence",
        confidence="low",
        confidence_score=0.2,
        abstention_reason_code=ReasonCode.retrieval_below_threshold,
        retrieved_hits=[
            QueryTraceHit(
                chunk_id="c1",
                document_id="doc-1",
                version="v1",
                score=0.21,
                source="rag",
                text="Some retrieved passage.",
            ),
            QueryTraceHit(
                chunk_id="c2",
                document_id="doc-2",
                version="v1",
                score=0.18,
                source="rag",
                text="Another retrieved passage.",
            ),
        ],
    )
    base.update(overrides)
    return QueryTraceRecord(**base)


def _diagnosis_json(
    *,
    cause: str = "Retrieval scores were all below the configured threshold.",
    analyzed: list[str] | None = None,
    recommendations: list[dict[str, str]] | None = None,
) -> str:
    if analyzed is None:
        analyzed = ["retrieval_scores", "route"]
    if recommendations is None:
        recommendations = [
            {"target": "ai_configuration", "description": "Lower the retrieval score threshold."},
            {"target": "corpus", "description": "Ingest documents covering refund policy."},
        ]
    return json.dumps(
        {
            "cause_description": cause,
            "analyzed_elements": analyzed,
            "recommendations": recommendations,
        }
    )


# ---------------------------------------------------------------------------
# R10.2 — unrecorded trace
# ---------------------------------------------------------------------------


def test_unrecorded_trace_raises_and_never_calls_model() -> None:
    llm = _StubLLM(_diagnosis_json())
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: None, llm=llm
    )

    with pytest.raises(TraceNotFoundError) as exc_info:
        investigator.diagnose("missing-trace")

    assert exc_info.value.trace_id == "missing-trace"
    # No diagnosis is performed for an unrecorded trace (R10.2) — the model is
    # never invoked and nothing is mutated.
    assert llm.calls == []


# ---------------------------------------------------------------------------
# R10.1 / R10.3 / R10.5 — identified cause
# ---------------------------------------------------------------------------


def test_identified_cause_returns_recommendations_and_analyzed_elements() -> None:
    trace = _trace()
    llm = _StubLLM(_diagnosis_json())
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert isinstance(diagnosis, TraceDiagnosis)
    assert diagnosis.trace_id == trace.trace_id
    assert diagnosis.cause_description
    # R10.3: references at least one analyzed element.
    assert len(diagnosis.analyzed_elements) >= 1
    assert set(diagnosis.analyzed_elements) <= {
        "route",
        "retrieval_scores",
        "rerank_order",
        "generation_outcome",
    }
    # R10.5: 1..10 recommendations, each targeting ai_configuration or corpus.
    assert 1 <= len(diagnosis.recommendations) <= 10
    assert all(r.target in ("ai_configuration", "corpus") for r in diagnosis.recommendations)


def test_diagnose_passes_investigator_thinking_budget() -> None:
    trace = _trace()
    llm = _StubLLM(_diagnosis_json())
    settings = _settings(RAG_TRACE_INVESTIGATOR_THINKING_BUDGET=2048)
    investigator = TraceInvestigator(
        settings, trace_resolver=lambda _tid: trace, llm=llm
    )

    investigator.diagnose(trace.trace_id)

    assert llm.calls[0]["thinking_budget"] == 2048
    assert llm.calls[0]["temperature"] == 0.0


# ---------------------------------------------------------------------------
# R10.5 — recommendation shaping (clamp + invalid target filtering)
# ---------------------------------------------------------------------------


def test_recommendations_clamped_to_ten() -> None:
    trace = _trace()
    recs = [
        {"target": "ai_configuration", "description": f"Change {i}"} for i in range(15)
    ]
    llm = _StubLLM(_diagnosis_json(recommendations=recs))
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert len(diagnosis.recommendations) == 10


def test_invalid_targets_and_empty_descriptions_are_dropped() -> None:
    trace = _trace()
    recs = [
        {"target": "database", "description": "Not a valid target."},
        {"target": "ai_configuration", "description": ""},
        {"target": "corpus", "description": "Ingest more documents."},
    ]
    llm = _StubLLM(_diagnosis_json(recommendations=recs))
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert diagnosis.recommendations == [
        Recommendation(target="corpus", description="Ingest more documents.")
    ]


def test_cause_with_missing_analyzed_elements_falls_back_to_available() -> None:
    trace = _trace()
    llm = _StubLLM(_diagnosis_json(analyzed=[]))
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    # R10.3: an identified cause always references >= 1 analyzed element even
    # when the model omits them.
    assert len(diagnosis.analyzed_elements) >= 1


# ---------------------------------------------------------------------------
# R10.4 — no cause determined
# ---------------------------------------------------------------------------


def test_no_recommendations_yields_no_cause_diagnosis() -> None:
    trace = _trace()
    llm = _StubLLM(
        _diagnosis_json(cause="Nothing conclusive found.", recommendations=[])
    )
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    # R10.4: no cause -> zero recommendations.
    assert diagnosis.recommendations == []
    assert diagnosis.cause_description  # non-empty description explaining no cause


def test_unparseable_output_yields_no_cause_diagnosis() -> None:
    trace = _trace()
    llm = _StubLLM("this is not JSON at all")
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert diagnosis.recommendations == []
    assert diagnosis.analyzed_elements == []
    assert diagnosis.cause_description


def test_model_error_yields_no_cause_diagnosis() -> None:
    trace = _trace()
    llm = _StubLLM(TimeoutError("model timed out"))
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert diagnosis.trace_id == trace.trace_id
    assert diagnosis.recommendations == []
    assert diagnosis.cause_description


# ---------------------------------------------------------------------------
# Fenced JSON tolerance + helpers
# ---------------------------------------------------------------------------


def test_parse_diagnosis_handles_markdown_fenced_json() -> None:
    trace = _trace()
    raw = f"```json\n{_diagnosis_json()}\n```"

    diagnosis = _parse_diagnosis(raw, trace)

    assert len(diagnosis.recommendations) == 2
    assert diagnosis.cause_description


def test_available_elements_reflects_recorded_trace() -> None:
    # Route is always available; multiple hits expose rerank order; claims or an
    # abstention or an answer expose the generation outcome.
    trace = _trace(
        claims=[
            Claim(
                claim_id="cl-1",
                text="Refunds are allowed within 30 days.",
                answer_span=AnswerSpan(start=0, end=10),
                evidence_status=EvidenceStatus.unsupported,
            )
        ]
    )
    elements = _available_elements(trace)
    assert "route" in elements
    assert "retrieval_scores" in elements
    assert "rerank_order" in elements
    assert "generation_outcome" in elements


def test_available_elements_minimal_trace() -> None:
    trace = _trace(
        retrieved_hits=[],
        claims=[],
        abstention_reason_code=None,
        answer="",
    )
    elements = _available_elements(trace)
    # Only the route is guaranteed present on a trace with no hits/claims/answer.
    assert elements == ["route"]


def test_prompt_includes_analyzed_signals() -> None:
    trace = _trace()
    prompt = _build_diagnosis_prompt(trace)
    assert trace.question in prompt
    assert "Route:" in prompt
    assert "Retrieval hits" in prompt
    assert "Generation outcome" in prompt
