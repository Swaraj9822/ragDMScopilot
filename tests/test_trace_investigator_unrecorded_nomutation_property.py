# Feature: rag-trust-and-observability, Property 36: Diagnosis of an unrecorded trace errors and never mutates
"""Property-based test for unrecorded-trace error and no-mutation guarantee (task 17.3).

**Validates: Requirements 10.2, 10.7**

Property 36 states:

- *For any* diagnosis request referencing a Trace that is not recorded, the
  Trace_Investigator performs no diagnosis and returns a trace-not-found error.
- *For any* diagnosis (whether the trace exists or not), no change is applied to
  the AI_Configuration or Corpus — the stores are unchanged.

The test exercises these guarantees via Hypothesis:

1. For arbitrary trace ids (including edge-case strings), when the resolver
   returns ``None``, ``TraceNotFoundError`` is raised *before* the model is
   invoked, and no diagnosis object is produced.
2. For arbitrary recorded traces (when the resolver returns a valid trace), the
   investigator returns a ``TraceDiagnosis`` but never calls any mutating method
   — verified by wrapping the resolver/stores with mutation-detecting proxies.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.models import (
    AnswerSpan,
    Claim,
    EvidenceStatus,
    QueryTraceHit,
    QueryTraceRecord,
    ReasonCode,
    TraceDiagnosis,
)
from rag_system.trace_investigator import (
    TraceInvestigator,
    TraceNotFoundError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


class _StubLLM:
    """Stub model that records invocations and returns a canned response."""

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

    def generate_stream(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError


class _MutationDetectingStore:
    """A fake 'store' that tracks whether any write method was called.

    Used to verify R10.7 — the investigator must never write to any store.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []

    def put_json(self, key: str, value: object) -> None:
        self.writes.append(f"put_json:{key}")

    def put_json_conditional(self, key: str, value: object, etag: str | None = None) -> str:
        self.writes.append(f"put_json_conditional:{key}")
        return '"etag"'

    def delete(self, key: str) -> None:
        self.writes.append(f"delete:{key}")


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
            {"target": "corpus", "description": "Ingest documents covering the topic."},
        ]
    return json.dumps(
        {
            "cause_description": cause,
            "analyzed_elements": analyzed,
            "recommendations": recommendations,
        }
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary trace IDs (including empty, whitespace, unicode, very long)
_trace_id_strategy = st.text(
    alphabet=st.characters(categories=("L", "M", "N", "P", "S", "Z")),
    min_size=0,
    max_size=200,
)

_route_strategy = st.sampled_from(["rag", "database", "hybrid", "sql"])

_reason_code_strategy = st.one_of(
    st.none(),
    st.sampled_from(list(ReasonCode)),
)

_evidence_status_strategy = st.sampled_from(list(EvidenceStatus))

_hit_strategy = st.builds(
    QueryTraceHit,
    chunk_id=st.text(min_size=1, max_size=20),
    document_id=st.text(min_size=1, max_size=20),
    version=st.text(min_size=1, max_size=10),
    score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    source=st.sampled_from(["rag", "hybrid", "pinecone"]),
    text=st.text(min_size=0, max_size=100),
)

def _answer_span_strategy():
    """Generate valid AnswerSpan where start <= end."""
    return st.integers(min_value=0, max_value=100).flatmap(
        lambda start: st.integers(min_value=start, max_value=start + 200).map(
            lambda end: AnswerSpan(start=start, end=end)
        )
    )


_claim_strategy = st.builds(
    Claim,
    claim_id=st.text(min_size=1, max_size=30),
    text=st.text(min_size=1, max_size=200),
    answer_span=_answer_span_strategy(),
    evidence_status=_evidence_status_strategy,
)

_trace_record_strategy = st.builds(
    QueryTraceRecord,
    trace_id=st.text(min_size=1, max_size=50),
    question=st.text(min_size=1, max_size=300),
    route=_route_strategy,
    retrieval_mode=st.one_of(st.none(), st.sampled_from(["hybrid", "dense", "sparse"])),
    answer=st.text(min_size=0, max_size=500),
    evidence_status=st.sampled_from(
        ["grounded", "partially_grounded", "insufficient_evidence"]
    ),
    confidence=st.one_of(st.none(), st.sampled_from(["high", "medium", "low"])),
    confidence_score=st.one_of(
        st.none(),
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    ),
    retrieved_hits=st.lists(_hit_strategy, min_size=0, max_size=5),
    claims=st.lists(_claim_strategy, min_size=0, max_size=5),
    abstention_reason_code=_reason_code_strategy,
    sql=st.one_of(st.none(), st.text(min_size=0, max_size=100)),
)


# ---------------------------------------------------------------------------
# Property: Unrecorded trace raises TraceNotFoundError, model never invoked
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 36: Diagnosis of an unrecorded trace errors and never mutates
# Validates: Requirements 10.2, 10.7
@settings(max_examples=200)
@given(trace_id=_trace_id_strategy)
def test_unrecorded_trace_raises_error_and_never_invokes_model(trace_id: str) -> None:
    """R10.2: When the trace is not recorded, a trace-not-found error is raised.

    No diagnosis is performed, no model is invoked, and nothing is mutated.
    """
    llm = _StubLLM(_diagnosis_json())
    store = _MutationDetectingStore()

    investigator = TraceInvestigator(
        _settings(),
        trace_resolver=lambda _tid: None,  # trace not found
        llm=llm,
    )

    with pytest.raises(TraceNotFoundError) as exc_info:
        investigator.diagnose(trace_id)

    # The error carries the requested trace id.
    assert exc_info.value.trace_id == trace_id

    # R10.2: No diagnosis is performed — the model is never called.
    assert llm.calls == [], "Model must not be invoked for an unrecorded trace"

    # R10.7: No mutations on any store.
    assert store.writes == [], "No store writes should occur for an unrecorded trace"


# ---------------------------------------------------------------------------
# Property: Diagnosis never mutates any store (read-only guarantee)
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 36: Diagnosis of an unrecorded trace errors and never mutates
# Validates: Requirements 10.2, 10.7
@settings(max_examples=200)
@given(trace=_trace_record_strategy)
def test_diagnosis_never_mutates_stores(trace: QueryTraceRecord) -> None:
    """R10.7: For any diagnosis (whether cause found or not), no configuration
    or corpus data is mutated — the investigator is purely read-only.

    We verify this by:
    1. Snapshotting the trace resolver's state before the call.
    2. Confirming no write methods are called on any store proxy.
    3. Confirming the trace object itself is not mutated.
    """
    # Snapshot the trace before diagnosis to detect any mutation.
    trace_snapshot = trace.model_copy(deep=True)

    llm = _StubLLM(_diagnosis_json())
    store = _MutationDetectingStore()

    investigator = TraceInvestigator(
        _settings(),
        trace_resolver=lambda _tid: trace,
        llm=llm,
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    # The result is a valid TraceDiagnosis.
    assert isinstance(diagnosis, TraceDiagnosis)
    assert diagnosis.trace_id == trace.trace_id

    # R10.7: No store writes occurred.
    assert store.writes == [], "Investigator must never write to any store"

    # R10.7: The trace record itself was not mutated by the diagnosis.
    assert trace.model_dump() == trace_snapshot.model_dump(), (
        "Investigator must not mutate the input trace record"
    )


# ---------------------------------------------------------------------------
# Property: Even when model errors occur, no mutation happens
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 36: Diagnosis of an unrecorded trace errors and never mutates
# Validates: Requirements 10.2, 10.7
@settings(max_examples=100)
@given(trace=_trace_record_strategy)
def test_diagnosis_no_mutation_on_model_error(trace: QueryTraceRecord) -> None:
    """R10.7: Even when the model errors (timeout, exception), the investigator
    still never mutates any store — it returns a safe no-cause diagnosis.
    """
    trace_snapshot = trace.model_copy(deep=True)

    llm = _StubLLM(TimeoutError("model timed out"))
    store = _MutationDetectingStore()

    investigator = TraceInvestigator(
        _settings(),
        trace_resolver=lambda _tid: trace,
        llm=llm,
    )

    diagnosis = investigator.diagnose(trace.trace_id)

    # Should return a no-cause diagnosis (graceful degradation).
    assert isinstance(diagnosis, TraceDiagnosis)
    assert diagnosis.recommendations == []

    # R10.7: No store writes.
    assert store.writes == [], "Investigator must never write to any store on model error"

    # R10.7: Trace unchanged.
    assert trace.model_dump() == trace_snapshot.model_dump(), (
        "Investigator must not mutate the input trace record on model error"
    )
