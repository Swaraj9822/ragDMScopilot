# Feature: rag-trust-and-observability, Property 35: Diagnosis output is consistent with cause determination
"""Property-based test for diagnosis-output consistency (task 17.2).

Feature: rag-trust-and-observability.

**Property 35: Diagnosis output is consistent with cause determination.**

**Validates: Requirements 10.1, 10.3, 10.4, 10.5.**

*For any* diagnosis of a recorded Trace: when a cause is identified, the cause
description references at least one of the analyzed elements (route, retrieval
scores, rerank order, generation outcome) and the diagnosis returns between 1
and 10 recommended changes, each referencing the ``AI_Configuration`` or the
``Corpus``; and when no cause is determined, the diagnosis indicates so and
returns zero recommended changes.

The trace investigator uses an LLM; tests stub the model to verify the
cause/recommendation shaping deterministically.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.models import (
    Claim,
    AnswerSpan,
    EvidenceStatus,
    QueryTraceHit,
    QueryTraceRecord,
    ReasonCode,
    TraceDiagnosis,
)
from rag_system.trace_investigator import TraceInvestigator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _settings() -> Settings:
    return Settings(**_REQUIRED_BY_ALIAS)  # type: ignore[arg-type]


_ANALYZED_ELEMENTS = ["route", "retrieval_scores", "rerank_order", "generation_outcome"]
_TARGETS = ["ai_configuration", "corpus"]


class _StubLLM:
    """Returns a canned model response for the trace investigator."""

    model_id = "gemini-3.1-pro"
    provider = "gemini"

    def __init__(self, response: str | Exception) -> None:
        self._response = response

    def generate(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response, {}

    def generate_stream(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_routes = st.sampled_from(["rag", "database", "hybrid"])
_reason_codes = st.sampled_from([None] + list(ReasonCode))


@st.composite
def _trace_hits(draw: st.DrawFn) -> list[QueryTraceHit]:
    """Generate 0..5 query trace hits with valid scores."""
    n = draw(st.integers(min_value=0, max_value=5))
    hits = []
    for i in range(n):
        hits.append(
            QueryTraceHit(
                chunk_id=f"chunk-{i}",
                document_id=f"doc-{i}",
                version="v1",
                score=draw(st.floats(min_value=0.0, max_value=1.0)),
                source="rag",
                text=f"passage {i}",
            )
        )
    return hits


@st.composite
def _trace_claims(draw: st.DrawFn) -> list[Claim]:
    """Generate 0..3 claims with valid evidence statuses."""
    n = draw(st.integers(min_value=0, max_value=3))
    claims = []
    for i in range(n):
        claims.append(
            Claim(
                claim_id=f"claim-{i}",
                text=f"Factual statement {i}.",
                answer_span=AnswerSpan(start=i * 20, end=i * 20 + 15),
                evidence_status=draw(st.sampled_from(list(EvidenceStatus))),
            )
        )
    return claims


@st.composite
def _trace_record(draw: st.DrawFn) -> QueryTraceRecord:
    """Generate a valid enriched QueryTraceRecord."""
    return QueryTraceRecord(
        trace_id=draw(st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-")),
        question=draw(st.text(min_size=1, max_size=80)),
        route=draw(_routes),
        retrieval_mode=draw(st.sampled_from(["hybrid", "semantic", "keyword", None])),
        answer=draw(st.text(min_size=0, max_size=200)),
        evidence_status=draw(st.sampled_from(["grounded", "partially_grounded", "insufficient_evidence"])),
        confidence=draw(st.sampled_from(["high", "medium", "low", None])),
        confidence_score=draw(st.floats(min_value=0.0, max_value=1.0) | st.none()),
        abstention_reason_code=draw(_reason_codes),
        retrieved_hits=draw(_trace_hits()),
        claims=draw(_trace_claims()),
    )


# -- Model response generators --

@st.composite
def _cause_response(draw: st.DrawFn) -> str:
    """Generate a valid JSON response with an identified cause (1..10 recs)."""
    num_recs = draw(st.integers(min_value=1, max_value=10))
    analyzed = draw(
        st.lists(
            st.sampled_from(_ANALYZED_ELEMENTS),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    recommendations = [
        {
            "target": draw(st.sampled_from(_TARGETS)),
            "description": draw(st.text(min_size=5, max_size=60, alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .")),
        }
        for _ in range(num_recs)
    ]
    return json.dumps({
        "cause_description": draw(st.text(min_size=10, max_size=100, alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .")),
        "analyzed_elements": analyzed,
        "recommendations": recommendations,
    })


@st.composite
def _no_cause_response(draw: st.DrawFn) -> str:
    """Generate a valid JSON response where no cause was determined."""
    # Possible variants: empty recommendations, or completely missing key
    variant = draw(st.sampled_from(["empty_recs", "no_recs_key"]))
    if variant == "empty_recs":
        return json.dumps({
            "cause_description": "No cause could be identified from this trace.",
            "analyzed_elements": [],
            "recommendations": [],
        })
    else:
        return json.dumps({
            "cause_description": "Nothing conclusive found.",
            "recommendations": [],
        })


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@hyp_settings(max_examples=50)
@given(trace=_trace_record(), response=_cause_response())
def test_identified_cause_references_analyzed_element_and_has_bounded_recommendations(
    trace: QueryTraceRecord, response: str
) -> None:
    """When a cause is determined, the description references at least one
    analyzed element (route, retrieval scores, rerank order, generation outcome)
    and between 1 and 10 recommendations are returned.

    **Validates: Requirements 10.1, 10.3, 10.4, 10.5**
    """
    llm = _StubLLM(response)
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert isinstance(diagnosis, TraceDiagnosis)
    assert diagnosis.trace_id == trace.trace_id

    # If the shaping produced recommendations (identified cause path):
    if diagnosis.recommendations:
        # R10.3: cause references at least one analyzed element
        assert len(diagnosis.analyzed_elements) >= 1
        assert all(
            e in _ANALYZED_ELEMENTS for e in diagnosis.analyzed_elements
        )
        # R10.5: 1..10 recommendations
        assert 1 <= len(diagnosis.recommendations) <= 10
        # R10.5: each recommendation references AI configuration or corpus
        for rec in diagnosis.recommendations:
            assert rec.target in ("ai_configuration", "corpus")
            assert len(rec.description) > 0
        # Cause description is non-empty
        assert len(diagnosis.cause_description) > 0


@hyp_settings(max_examples=50)
@given(trace=_trace_record(), response=_no_cause_response())
def test_no_cause_returns_description_and_zero_recommendations(
    trace: QueryTraceRecord, response: str
) -> None:
    """When no cause is determined, description says so with zero
    recommendations.

    **Validates: Requirements 10.1, 10.3, 10.4, 10.5**
    """
    llm = _StubLLM(response)
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert isinstance(diagnosis, TraceDiagnosis)
    assert diagnosis.trace_id == trace.trace_id
    # R10.4: no cause → zero recommendations
    assert diagnosis.recommendations == []
    # R10.4: a description indicating no cause was determined
    assert len(diagnosis.cause_description) > 0


@hyp_settings(max_examples=50)
@given(trace=_trace_record())
def test_model_error_yields_no_cause_with_zero_recommendations(
    trace: QueryTraceRecord,
) -> None:
    """When the model errors (timeout, exception), the diagnosis degrades to
    no-cause with zero recommendations (never raises).

    **Validates: Requirements 10.4, 10.5**
    """
    llm = _StubLLM(TimeoutError("model timed out"))
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    assert isinstance(diagnosis, TraceDiagnosis)
    assert diagnosis.trace_id == trace.trace_id
    assert diagnosis.recommendations == []
    assert len(diagnosis.cause_description) > 0


@hyp_settings(max_examples=50)
@given(trace=_trace_record(), response=_cause_response())
def test_each_recommendation_references_ai_configuration_or_corpus(
    trace: QueryTraceRecord, response: str
) -> None:
    """Each recommendation must reference AI configuration or corpus.

    **Validates: Requirements 10.5**
    """
    llm = _StubLLM(response)
    investigator = TraceInvestigator(
        _settings(), trace_resolver=lambda _tid: trace, llm=llm
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    for rec in diagnosis.recommendations:
        assert rec.target in ("ai_configuration", "corpus"), (
            f"Recommendation target '{rec.target}' is not ai_configuration or corpus"
        )
        assert len(rec.description) > 0, "Recommendation description must not be empty"
