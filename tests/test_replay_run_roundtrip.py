"""Round-trip property test for ReplayRun / ReplayRunResult serialization (R8.10).

# Feature: rag-trust-and-observability, task 14.9: replay status endpoint and round-trip coverage

Verifies that serializing (``model_dump`` / ``model_dump_json``) and
deserializing a :class:`ReplayRun` (with an optional :class:`ReplayRunResult`
containing discriminated ``EvidenceItem``s of both ``document`` and ``database``
kinds) preserves all fields exactly. This guarantees the ``GET /replays/{id}``
endpoint can persist and return a run without data loss.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.models import (
    EvidenceCoverage,
    EvidenceItem,
    ReplayRetrievalParams,
    ReplayRun,
    ReplayRunRequest,
    ReplayRunResult,
    ReplayRunState,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_non_empty_text = st.text(min_size=1, max_size=30)
_text = st.text(min_size=0, max_size=30)

_verification_results = st.sampled_from(list(VerificationResult))
_coverage_values = st.sampled_from(list(EvidenceCoverage))

_document_evidence = st.builds(
    EvidenceItem,
    kind=st.just("document"),
    verification_result=_verification_results,
    coverage=_coverage_values,
    covered_subclaims=st.lists(st.integers(min_value=0, max_value=99), max_size=5),
    quote=_non_empty_text,
    source_start=st.integers(min_value=0, max_value=1000),
    source_end=st.integers(min_value=0, max_value=1000),
    document_id=_non_empty_text,
    document_version=_non_empty_text,
)

_database_evidence = st.builds(
    EvidenceItem,
    kind=st.just("database"),
    verification_result=_verification_results,
    coverage=_coverage_values,
    covered_subclaims=st.lists(st.integers(min_value=0, max_value=99), max_size=5),
    table=_non_empty_text,
    row_fields=st.dictionaries(
        keys=_non_empty_text,
        values=st.one_of(
            st.text(max_size=20),
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=5,
    ),
    sql=st.one_of(st.none(), _non_empty_text),
    sql_query_id=st.one_of(st.none(), _non_empty_text),
    sql_result_fixture_id=st.one_of(st.none(), _non_empty_text),
    row_index=st.one_of(st.none(), st.integers(min_value=0, max_value=999)),
)

_evidence_items = st.lists(
    st.one_of(_document_evidence, _database_evidence),
    min_size=0,
    max_size=10,
)

_replay_run_results = st.builds(
    ReplayRunResult,
    answer=_non_empty_text,
    evidence=_evidence_items,
    route=st.sampled_from(["rag", "database", "hybrid"]),
    retrieval_scores=st.lists(
        st.floats(min_value=0.0, max_value=1.0), min_size=0, max_size=10
    ),
    latency_ms=st.floats(min_value=0.0, max_value=300_000.0),
    prompt_tokens=st.integers(min_value=0, max_value=100_000),
    completion_tokens=st.integers(min_value=0, max_value=100_000),
    cost=st.floats(min_value=0.0, max_value=100.0),
)

_replay_run_requests = st.builds(
    ReplayRunRequest,
    question=_non_empty_text,
    ai_configuration_version_id=_non_empty_text,
    retrieval_params=st.builds(
        ReplayRetrievalParams,
        max_passages=st.integers(min_value=1, max_value=100),
        min_score=st.floats(min_value=0.0, max_value=1.0),
    ),
    corpus_snapshot_id=_non_empty_text,
)

_replay_runs = st.builds(
    ReplayRun,
    replay_run_id=_non_empty_text,
    state=st.sampled_from(list(ReplayRunState)),
    request=_replay_run_requests,
    result=st.one_of(st.none(), _replay_run_results),
    failure_reason=st.one_of(st.none(), _non_empty_text),
    cancel_requested=st.booleans(),
)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(run=_replay_runs)
def test_replay_run_python_round_trip(run: ReplayRun) -> None:
    """Validates: Requirements 8.10

    Serializing a ReplayRun via model_dump and deserializing it back preserves
    all fields (state, request, result, failure_reason, cancel_requested).
    """
    restored = ReplayRun.model_validate(run.model_dump())
    assert restored == run
    assert restored.state == run.state
    assert restored.replay_run_id == run.replay_run_id
    assert restored.cancel_requested == run.cancel_requested
    if run.result is not None:
        assert restored.result is not None
        assert restored.result.answer == run.result.answer
        assert restored.result.evidence == run.result.evidence
        assert restored.result.route == run.result.route
        assert restored.result.retrieval_scores == run.result.retrieval_scores
        assert restored.result.latency_ms == run.result.latency_ms
        assert restored.result.prompt_tokens == run.result.prompt_tokens
        assert restored.result.completion_tokens == run.result.completion_tokens
        assert restored.result.cost == run.result.cost
    else:
        assert restored.result is None


@settings(max_examples=200)
@given(run=_replay_runs)
def test_replay_run_json_round_trip(run: ReplayRun) -> None:
    """Validates: Requirements 8.10

    JSON string round-trip (model_dump_json → model_validate_json) preserves
    all fields, which is the actual wire format the GET /replays/{id} endpoint
    serves.
    """
    restored = ReplayRun.model_validate_json(run.model_dump_json())
    assert restored == run
    assert restored.state == run.state
    assert restored.replay_run_id == run.replay_run_id
    assert restored.request == run.request
    assert restored.failure_reason == run.failure_reason
    assert restored.cancel_requested == run.cancel_requested
    if run.result is not None:
        assert restored.result is not None
        assert restored.result == run.result
    else:
        assert restored.result is None
