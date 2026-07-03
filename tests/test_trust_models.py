"""Unit and property tests for the RAG trust & observability data models.

Feature: rag-trust-and-observability (task 1.1 — backend data models).

These cover the model-level guarantees the design relies on: the per-kind
``EvidenceItem`` discriminated validator, ``AnswerSpan`` bounds, the additive
extensions to ``QueryResponse`` / ``UnifiedQueryResponse`` / ``QueryTraceRecord``,
and serialize -> deserialize round-trips for the discriminated ``EvidenceItem``
across both ``document`` and ``database`` kinds. These are model-shape tests
only; the numbered correctness properties live in their own tasks.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from rag_system.models import (
    AbstentionResponse,
    AIConfigurationIndex,
    AIConfigurationVersion,
    AnswerSpan,
    BenchmarkCase,
    Claim,
    ClarificationPrompt,
    ClarificationRecord,
    CorpusSnapshot,
    DocumentRecord,
    DocumentStatus,
    DocumentVersionIndex,
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    FailureCategory,
    FeedbackContext,
    FeedbackReviewRecord,
    KnowledgeGapMap,
    QueryResponse,
    QueryTraceRecord,
    ReasonCode,
    ReplayRun,
    ReplayRetrievalParams,
    ReplayRunRequest,
    ReplayRunState,
    ReviewStatus,
    SqlResultFixture,
    TraceDiagnosis,
    UnifiedQueryResponse,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# EvidenceItem discriminated validator
# ---------------------------------------------------------------------------


def _valid_document_item(**overrides) -> dict:
    base = dict(
        kind="document",
        verification_result=VerificationResult.entails,
        coverage=EvidenceCoverage.full,
        quote="the sky is blue",
        source_start=0,
        source_end=15,
        document_id="doc-1",
        document_version="v1",
    )
    base.update(overrides)
    return base


def _valid_database_item(**overrides) -> dict:
    base = dict(
        kind="database",
        verification_result=VerificationResult.entails,
        coverage=EvidenceCoverage.full,
        table="orders",
        row_fields={"id": 1, "total": 42},
        sql="SELECT * FROM orders",
        row_index=0,
    )
    base.update(overrides)
    return base


def test_document_evidence_item_valid():
    item = EvidenceItem(**_valid_document_item())
    assert item.kind == "document"
    assert item.document_id == "doc-1"


@pytest.mark.parametrize(
    "missing",
    ["quote", "source_start", "source_end", "document_id", "document_version"],
)
def test_document_evidence_item_requires_fields(missing):
    payload = _valid_document_item()
    payload[missing] = None
    with pytest.raises(ValidationError):
        EvidenceItem(**payload)


def test_database_evidence_item_valid():
    item = EvidenceItem(**_valid_database_item())
    assert item.kind == "database"
    assert item.row_fields == {"id": 1, "total": 42}


@pytest.mark.parametrize("missing", ["table", "row_fields"])
def test_database_evidence_item_requires_fields(missing):
    payload = _valid_database_item()
    payload[missing] = None
    with pytest.raises(ValidationError):
        EvidenceItem(**payload)


def test_database_item_does_not_require_document_fields():
    # A database item legitimately carries no quote/offsets/document_id.
    item = EvidenceItem(**_valid_database_item())
    assert item.quote is None
    assert item.document_id is None


# ---------------------------------------------------------------------------
# AnswerSpan bounds
# ---------------------------------------------------------------------------


def test_answer_span_allows_equal_and_forward():
    assert AnswerSpan(start=0, end=0).end == 0
    assert AnswerSpan(start=2, end=5).start == 2


def test_answer_span_rejects_negative():
    with pytest.raises(ValidationError):
        AnswerSpan(start=-1, end=3)


def test_answer_span_rejects_end_before_start():
    with pytest.raises(ValidationError):
        AnswerSpan(start=5, end=2)


# ---------------------------------------------------------------------------
# Claim + response extensions
# ---------------------------------------------------------------------------


def test_claim_carries_evidence_and_status():
    claim = Claim(
        claim_id="c1",
        text="the sky is blue",
        answer_span=AnswerSpan(start=0, end=15),
        evidence_items=[EvidenceItem(**_valid_document_item())],
        evidence_status=EvidenceStatus.supported,
    )
    assert claim.evidence_items[0].kind == "document"
    assert claim.evidence_status == EvidenceStatus.supported


def test_query_response_claim_defaults():
    resp = QueryResponse(
        answer="a", citations=[], evidence_status="grounded", trace_id="t1"
    )
    assert resp.claims == []
    assert resp.claim_decomposition_failed is False


def test_unified_query_response_claim_defaults():
    resp = UnifiedQueryResponse(
        answer="a", route="rag", evidence_status="grounded", trace_id="t1"
    )
    assert resp.claims == []
    assert resp.claim_decomposition_failed is False


def test_query_trace_record_additive_fields_default():
    rec = QueryTraceRecord(
        trace_id="t1",
        question="q",
        route="rag",
        answer="a",
        evidence_status="grounded",
    )
    assert rec.sql is None
    assert rec.claims == []
    assert rec.claim_evidence_summary == {}
    assert rec.ai_configuration_version_id is None
    assert rec.cost is None
    assert rec.abstention_reason_code is None
    assert rec.is_clarification is False


def test_document_record_owner_optional():
    rec = DocumentRecord(
        id="d1", title="t", version="v1", s3_uri="s3://x", status=DocumentStatus.indexed
    )
    assert rec.owner is None
    assert DocumentRecord(
        id="d1",
        title="t",
        version="v1",
        s3_uri="s3://x",
        status=DocumentStatus.indexed,
        owner="alice",
    ).owner == "alice"


# ---------------------------------------------------------------------------
# Abstention / clarification / other artifacts smoke construction
# ---------------------------------------------------------------------------


def test_abstention_response_bounds():
    resp = AbstentionResponse(
        reason_code=ReasonCode.no_evidence, missing_information="x", trace_id="t1"
    )
    assert resp.reason_code == ReasonCode.no_evidence
    with pytest.raises(ValidationError):
        AbstentionResponse(
            reason_code=ReasonCode.no_evidence, missing_information="", trace_id="t1"
        )
    with pytest.raises(ValidationError):
        AbstentionResponse(
            reason_code=ReasonCode.no_evidence,
            missing_information="x" * 1001,
            trace_id="t1",
        )


def test_clarification_models():
    rec = ClarificationRecord(
        clarification_id="cid",
        conversation_turn_id="turn-1",
        original_question="which one?",
        clarification_expiry="2024-01-01T00:00:00Z",
    )
    assert rec.document_scope is None
    prompt = ClarificationPrompt(
        clarification_question="Do you mean A or B?",
        clarification_id="cid",
        conversation_turn_id="turn-1",
        clarification_expiry="2024-01-01T00:00:00Z",
    )
    assert prompt.clarification_id == "cid"


def test_feedback_review_record_and_context():
    rec = FeedbackReviewRecord(
        trace_id="t1", feedback_id="f1", created_at="2024-01-01T00:00:00Z", rating=1
    )
    assert rec.review_status == ReviewStatus.unreviewed
    assert rec.failure_category is None
    ctx = FeedbackContext(feedback=rec)
    assert ctx.retrieved_chunks == []
    # Enum carries human-readable values.
    assert FailureCategory.missing_knowledge.value == "Missing knowledge"


def test_benchmark_case_extends_golden_case():
    case = BenchmarkCase(id="b1", question="q")
    assert case.human_reviewed is False
    assert case.relevance_labels is None
    # Inherited GoldenCase field is present.
    assert case.required_answer_terms == []


def test_replay_retrieval_params_bounds():
    ReplayRetrievalParams(max_passages=10, min_score=0.5)
    with pytest.raises(ValidationError):
        ReplayRetrievalParams(max_passages=0, min_score=0.5)
    with pytest.raises(ValidationError):
        ReplayRetrievalParams(max_passages=10, min_score=1.5)


def test_replay_run_defaults():
    run = ReplayRun(
        replay_run_id="r1",
        state=ReplayRunState.queued,
        request=ReplayRunRequest(
            question="q",
            ai_configuration_version_id="v1",
            retrieval_params=ReplayRetrievalParams(max_passages=5, min_score=0.2),
            corpus_snapshot_id="snap-1",
        ),
    )
    assert run.result is None
    assert run.cancel_requested is False


def test_corpus_snapshot_and_fixture():
    snap = CorpusSnapshot(
        corpus_snapshot_id="snap-1",
        created_at="2024-01-01T00:00:00Z",
        manifest=[("doc-1", "v1"), ("doc-2", "v3")],
    )
    assert snap.manifest[0] == ("doc-1", "v1")
    fixture = SqlResultFixture(
        fixture_id="fx-1",
        corpus_snapshot_id="snap-1",
        sql="SELECT 1",
        normalized_sql_hash="abc123",
        rows=[{"n": 1}],
    )
    assert fixture.normalized_sql_hash == "abc123"


def test_ai_configuration_models():
    ver = AIConfigurationVersion(
        config_id="cfg",
        version_id="v1",
        prompt="p",
        model="gemini-3.5-flash",
        router_threshold=0.5,
        change_description="initial",
        created_at="2024-01-01T00:00:00Z",
    )
    assert ver.approved is False
    with pytest.raises(ValidationError):
        AIConfigurationVersion(
            config_id="cfg",
            version_id="v2",
            prompt="p",
            model="m",
            router_threshold=0.5,
            change_description="",  # min_length=1
            created_at="2024-01-01T00:00:00Z",
        )
    idx = AIConfigurationIndex(config_id="cfg")
    assert idx.active_version_id is None
    assert idx.versions == []


def test_version_index_and_gap_and_diagnosis():
    idx = DocumentVersionIndex(document_id="d1")
    assert idx.active_version is None
    diag = TraceDiagnosis(trace_id="t1", cause_description="root cause")
    assert diag.recommendations == []
    gap = KnowledgeGapMap(eligible_outcome_count=5, configured_minimum=20)
    assert gap.topics == []


# ---------------------------------------------------------------------------
# Round-trip property for the discriminated EvidenceItem (both kinds)
# ---------------------------------------------------------------------------

_verification = st.sampled_from(list(VerificationResult))
_coverage = st.sampled_from(list(EvidenceCoverage))
_subclaims = st.lists(st.integers(min_value=0, max_value=50), max_size=5)


@st.composite
def _document_items(draw):
    text = draw(st.text(min_size=1, max_size=40))
    start = draw(st.integers(min_value=0, max_value=100))
    end = draw(st.integers(min_value=start, max_value=start + 100))
    return EvidenceItem(
        kind="document",
        verification_result=draw(_verification),
        coverage=draw(_coverage),
        covered_subclaims=draw(_subclaims),
        quote=text,
        source_start=start,
        source_end=end,
        document_id=draw(st.text(min_size=1, max_size=12)),
        document_version=draw(st.text(min_size=1, max_size=8)),
    )


@st.composite
def _database_items(draw):
    keys = draw(st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=4, unique=True))
    row_fields = {k: draw(st.integers(min_value=-100, max_value=100)) for k in keys}
    return EvidenceItem(
        kind="database",
        verification_result=draw(_verification),
        coverage=draw(_coverage),
        covered_subclaims=draw(_subclaims),
        table=draw(st.text(min_size=1, max_size=12)),
        row_fields=row_fields,
        sql=draw(st.one_of(st.none(), st.text(max_size=30))),
        row_index=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=1000))),
    )


@given(st.one_of(_document_items(), _database_items()))
def test_evidence_item_round_trip(item: EvidenceItem):
    restored = EvidenceItem.model_validate(item.model_dump())
    assert restored == item
    assert restored.kind == item.kind
