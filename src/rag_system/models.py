from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Claims and evidence (R1)
# ---------------------------------------------------------------------------


class VerificationResult(StrEnum):
    entails = "entails"
    does_not_entail = "does_not_entail"
    undetermined = "undetermined"


class EvidenceCoverage(StrEnum):
    """How much of a claim a (claim, evidence) pair covers (R1.5)."""

    full = "full"
    partial = "partial"
    none = "none"


class EvidenceStatus(StrEnum):
    supported = "supported"
    partially_supported = "partially_supported"
    unsupported = "unsupported"
    verification_unavailable = "verification_unavailable"


class AnswerSpan(BaseModel):
    #: Zero-based, inclusive start offset into the answer text.
    start: int = Field(ge=0)
    #: Zero-based, exclusive end offset into the answer text; ``end >= start``.
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "AnswerSpan":
        if self.end < self.start:
            raise ValueError("answer span end must be >= start")
        return self


class EvidenceItem(BaseModel):
    """A single piece of evidence associated with a claim.

    Discriminated by ``kind`` so a document passage and a database row both fit
    naturally; document fields are optional because a ``database`` item has none
    (and vice versa). The per-kind ``model_validator`` enforces the required
    fields for each discriminant.
    """

    kind: Literal["document", "database"]
    verification_result: VerificationResult
    #: Drives partial support (R1.5).
    coverage: EvidenceCoverage = EvidenceCoverage.none
    #: Optional sub-claim indices this item covers.
    covered_subclaims: list[int] = Field(default_factory=list)

    # kind == "document"
    quote: str | None = None
    source_start: int | None = None
    source_end: int | None = None
    document_id: str | None = None
    document_version: str | None = None

    # kind == "database"
    table: str | None = None
    row_fields: dict[str, Any] | None = None
    sql: str | None = None
    sql_query_id: str | None = None
    #: Nullable for live / unfixtured rows.
    sql_result_fixture_id: str | None = None
    row_index: int | None = None

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "EvidenceItem":
        # document → requires quote + offsets + document_id + document_version;
        # database → requires table + row_fields.
        if self.kind == "document":
            if (
                self.quote is None
                or self.source_start is None
                or self.source_end is None
                or self.document_id is None
                or self.document_version is None
            ):
                raise ValueError(
                    "document evidence requires quote, source offsets, "
                    "document_id, and document_version"
                )
        else:  # database
            if self.table is None or self.row_fields is None:
                raise ValueError("database evidence requires table and row_fields")
        return self


class Claim(BaseModel):
    claim_id: str
    text: str
    answer_span: AnswerSpan
    #: 0..100 evidence items associated with this claim.
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    evidence_status: EvidenceStatus


# ---------------------------------------------------------------------------
# Abstention (R3)
# ---------------------------------------------------------------------------


class ReasonCode(StrEnum):
    low_confidence = "low_confidence"
    no_evidence = "no_evidence"
    unsupported_claims = "unsupported_claims"
    conflicting_evidence = "conflicting_evidence"
    sql_no_rows = "sql_no_rows"
    retrieval_below_threshold = "retrieval_below_threshold"


class AbstentionResponse(BaseModel):
    """Returned instead of an answer. Carries no answer/claims/evidence (R3.7)."""

    reason_code: ReasonCode
    missing_information: str = Field(min_length=1, max_length=1000)
    trace_id: str


class DocumentStatus(StrEnum):
    queued = "queued"
    parsing = "parsing"
    chunking = "chunking"
    embedding = "embedding"
    indexed = "indexed"
    failed = "failed"
    deleted = "deleted"


class DocumentRecord(BaseModel):
    id: str
    title: str
    version: str
    s3_uri: str
    status: DocumentStatus
    error: str | None = None
    #: The version whose vectors are currently published (searchable). It is set
    #: atomically only after a full ingestion succeeds, so partial/in-flight
    #: vectors of a different version are never treated as searchable. ``None``
    #: means nothing has been published yet (first ingestion in flight). Records
    #: written before this field existed fall back to ``version`` when their
    #: status is ``indexed`` (see ``RagService._active_version_for``).
    active_version: str | None = None
    #: Authenticated identity that owns the document. Used for owner-based
    #: corpus scoping for non-operators (R4.11). ``None`` for legacy records.
    owner: str | None = None


class ParsedDocument(BaseModel):
    document_id: str
    version: str
    markdown: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    id: str
    document_id: str
    version: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddedChunk(BaseModel):
    chunk: Chunk
    dense_vector: list[float]
    sparse_vector: dict[str, Any] | None = None


class Citation(BaseModel):
    document_id: str
    chunk_id: str
    page_start: int | None = None
    page_end: int | None = None
    title: str | None = None


class RetrievalHit(BaseModel):
    chunk: Chunk
    score: float
    source: str

    @field_validator("score")
    @classmethod
    def _score_must_be_finite(cls, value: float) -> float:
        """Reject NaN/±inf scores.

        Similarity scores are metric-dependent and legitimately unbounded (a
        ``dotproduct`` index can return negatives or values above 1), so no
        ``[0, 1]`` range is imposed. But a non-finite score would silently
        corrupt downstream confidence math and ordering, so it is rejected here.
        """
        if not isfinite(value):
            raise ValueError(f"retrieval score must be a finite number, got {value!r}")
        return value


#: Upper bounds on user-supplied query inputs. A question has no legitimate need
#: to exceed a few thousand characters, and an unbounded value would flow
#: straight into (paid, latency-bound) LLM prompts; the document-scope list is
#: likewise capped so a request body cannot grow without limit. Both guard the
#: answer path against accidental or abusive oversized payloads.
MAX_QUESTION_CHARS = 16_000
MAX_DOCUMENT_IDS = 1_000


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    document_ids: list[str] | None = Field(default=None, max_length=MAX_DOCUMENT_IDS)


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    evidence_status: str
    trace_id: str
    confidence: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_evidence_reason: str | None = None
    #: Claim-level evidence mapping (R1.14). Empty when decomposition failed.
    claims: list[Claim] = Field(default_factory=list)
    claim_decomposition_failed: bool = False
    #: Retrieval scores of the hits that grounded this answer, in hit order
    #: (R3.2/R3.6). Empty when retrieval returned nothing; consumed by the
    #: abstention gate to fire ``no_evidence`` / ``retrieval_below_threshold``.
    retrieval_scores: list[float] = Field(default_factory=list)


class CopilotQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    include_sql: bool = False


class CopilotDataSource(BaseModel):
    table: str
    columns: list[str]


class CopilotQueryResponse(BaseModel):
    answer: str
    mode: str
    evidence_status: str
    trace_id: str
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data_sources: list[CopilotDataSource] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class UnifiedQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    document_ids: list[str] | None = Field(default=None, max_length=MAX_DOCUMENT_IDS)
    include_sql: bool = False
    #: Server-side conversation this turn belongs to. ``None`` starts a new
    #: conversation; the id to continue is returned on the response.
    conversation_id: str | None = None
    #: When true, ignore prior turns for this request (treat the question as a
    #: fresh, standalone start) and clear the conversation's accumulated context
    #: while keeping the same conversation id and document scope.
    forget_context: bool = False


class UnifiedQueryResponse(BaseModel):
    answer: str
    route: str
    evidence_status: str
    trace_id: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_evidence_reason: str | None = None
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data_sources: list[CopilotDataSource] = Field(default_factory=list)
    routing_reasoning: str | None = None
    #: Conversation this answer was recorded against (always set when the
    #: conversation manager is active). The client echoes it on the next turn.
    conversation_id: str | None = None
    #: The standalone query the follow-up was rewritten into, surfaced for
    #: transparency. ``None`` when the question was used verbatim (first turn or
    #: already self-contained).
    rewritten_question: str | None = None
    #: Claim-level evidence mapping (R1.14). Empty when decomposition failed.
    claims: list[Claim] = Field(default_factory=list)
    claim_decomposition_failed: bool = False
    #: Retrieval scores of the hits that grounded a RAG answer, in hit order
    #: (R3.2/R3.6). Empty for non-retrieval routes; consumed by the abstention
    #: gate for the ``rag`` route.
    retrieval_scores: list[float] = Field(default_factory=list)


class ConversationTurn(BaseModel):
    """A single recorded question/answer exchange within a conversation."""

    question: str
    #: The standalone query actually sent to retrieval/routing. Equals
    #: ``question`` when no rewrite was needed.
    standalone_question: str
    answer: str
    route: str
    trace_id: str
    asked_at: str


class ConversationRecord(BaseModel):
    """Server-side multi-turn conversation state.

    Persisted per conversation so follow-ups can be rewritten against prior
    turns and the selected-document scope is preserved across the session.
    """

    conversation_id: str
    created_at: str
    updated_at: str
    #: The document scope carried across turns. Inherited by a follow-up that
    #: does not specify its own ``document_ids``.
    document_ids: list[str] | None = None
    turns: list[ConversationTurn] = Field(default_factory=list)


class QueryTraceHit(BaseModel):
    chunk_id: str
    document_id: str
    version: str
    score: float
    source: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    title: str | None = None
    section_path: list[str] = Field(default_factory=list)


class QueryTraceRecord(BaseModel):
    trace_id: str
    question: str
    route: str
    retrieval_mode: str | None = None
    document_ids: list[str] | None = None
    answer: str
    evidence_status: str
    confidence: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_evidence_reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    retrieved_hits: list[QueryTraceHit] = Field(default_factory=list)
    model_ids: dict[str, str] = Field(default_factory=dict)
    latency_ms: float | None = None
    #: --- Additive fields for downstream consumers (R6, R7, R8, R10, R11) ---
    #: SQL-route query text (R6.2, R10.1).
    sql: str | None = None
    #: Claim-level evidence mapping (R1, R10.1).
    claims: list[Claim] = Field(default_factory=list)
    #: Counts per ``EvidenceStatus`` for quick summaries.
    claim_evidence_summary: dict[str, int] = Field(default_factory=dict)
    #: Producing AI configuration version (R9.1); ``None`` => unresolved (R9.2).
    ai_configuration_version_id: str | None = None
    #: Computed answer cost for comparison (R8.7-style).
    cost: float | None = None
    #: Set when the turn abstained (R3, R11 eligibility).
    abstention_reason_code: ReasonCode | None = None
    #: True when the turn returned a Clarification_Prompt (R2).
    is_clarification: bool = False


class QueryFeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)
    expected_answer: str | None = Field(default=None, max_length=5000)


class QueryFeedbackRecord(QueryFeedbackRequest):
    trace_id: str
    feedback_id: str
    created_at: str


# ---------------------------------------------------------------------------
# Clarification (R2)
# ---------------------------------------------------------------------------


class ClarificationRecord(BaseModel):
    """Persisted (create-only) binding of an unguessable clarification id to a
    conversation turn, document scope, original question, and expiry (R2.2)."""

    clarification_id: str
    conversation_turn_id: str
    original_question: str
    document_scope: list[str] | None = None
    #: ISO-8601 UTC expiry timestamp.
    clarification_expiry: str


class ClarificationPrompt(BaseModel):
    clarification_question: str
    clarification_id: str
    conversation_turn_id: str
    clarification_expiry: str
    document_scope: list[str] | None = None


class ClarificationReplyRequest(BaseModel):
    """Request body for POST /ask/clarify (R2.4–R2.6)."""

    clarification_id: str = Field(min_length=1)
    reply: str


# ---------------------------------------------------------------------------
# Corpus & versions (R4, R5)
# ---------------------------------------------------------------------------


class DocumentVersion(BaseModel):
    document_id: str
    version: str
    created_at: str
    indexed: bool
    vectors_present: bool
    #: All version source content is retained, including superseded (R5.5).
    source_retained: bool = True


class IngestionEvent(BaseModel):
    ingestion_id: str
    document_id: str
    version: str
    status: Literal["succeeded", "failed"]
    timestamp: str
    error: str | None = None


class DocumentVersionIndex(BaseModel):
    document_id: str
    #: At most one active version per document (R5.4).
    active_version: str | None = None
    versions: list[DocumentVersion] = Field(default_factory=list)


class CorpusPage(BaseModel):
    documents: list[DocumentRecord] = Field(default_factory=list)
    #: ``None`` on the final page (R4.4).
    next_cursor: str | None = None


class DocumentHistory(BaseModel):
    """A Document's version history and ingestion events (R5.7).

    Both ``versions`` and ``events`` are ordered by ingestion timestamp, most
    recent first, so the frontend can render newest-first without re-sorting.
    """

    document_id: str
    #: The currently active version, or ``None`` when nothing is indexed (R5.4).
    active_version: str | None = None
    versions: list[DocumentVersion] = Field(default_factory=list)
    events: list[IngestionEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Feedback (R6)
# ---------------------------------------------------------------------------


class ReviewStatus(StrEnum):
    unreviewed = "unreviewed"
    reviewed = "reviewed"
    resolved = "resolved"


class FailureCategory(StrEnum):
    missing_knowledge = "Missing knowledge"
    retrieval_failure = "Retrieval failure"
    wrong_route = "Wrong route"
    unsupported_answer = "Unsupported answer"
    sql_problem = "SQL problem"
    ambiguous_question = "Ambiguous question"


class FeedbackReviewRecord(QueryFeedbackRecord):
    review_status: ReviewStatus = ReviewStatus.unreviewed
    failure_category: FailureCategory | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    #: De-dup guard for promotion (R6.11).
    promoted_case_id: str | None = None


class FeedbackContext(BaseModel):
    feedback: FeedbackReviewRecord
    expected_answer: str | None = None
    confidence: str | None = None
    route: str | None = None
    retrieved_chunks: list[QueryTraceHit] = Field(default_factory=list)
    sql: str | None = None


class FeedbackInboxPage(BaseModel):
    """One cursor-paginated page of the negative-rating feedback inbox (R6.1)."""

    items: list[FeedbackContext] = Field(default_factory=list)
    #: ``None`` on the final page (R6.1).
    next_cursor: str | None = None


class FeedbackClassifyRequest(BaseModel):
    """Request body for ``POST /feedback/{id}/classify`` (R6.5, R6.10)."""

    category: str = Field(..., description="One of the six Failure_Category values.")


# ---------------------------------------------------------------------------
# Evaluation (R7)
# ---------------------------------------------------------------------------


class RelevanceLabels(BaseModel):
    relevant_chunk_ids: list[str] = Field(default_factory=list)
    relevant_document_ids: list[str] = Field(default_factory=list)
    human_judgments: dict[str, float] = Field(default_factory=dict)


class DeterministicCheck(BaseModel):
    name: str
    outcome: Literal["pass", "fail"]


class RetrievalMetrics(BaseModel):
    recall_at_k: float
    precision_at_k: float
    mrr_at_k: float
    depth: int


class LLMJudgeScores(BaseModel):
    faithfulness: float | None = Field(default=None, ge=0.0, le=1.0)
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Set on per-case timeout (R7.8).
    error: str | None = None


class BenchmarkResult(BaseModel):
    case_id: str
    deterministic_checks: list[DeterministicCheck] = Field(default_factory=list)
    #: ``None`` when no relevance labels are present (R7.9).
    retrieval_metrics: RetrievalMetrics | None = None
    llm_judge: LLMJudgeScores | None = None


class EvaluationRunSummary(BaseModel):
    """Listing row for a persisted evaluation run (R7.7)."""

    run_id: str
    created_at: str
    #: CI pass/fail decided solely by deterministic checks (R7.5, R7.6).
    ci_passed: bool
    result_count: int


class EvaluationRunDetail(BaseModel):
    """Full detail of a persisted evaluation run, including per-case results."""

    run_id: str
    created_at: str
    ci_passed: bool
    results: list[BenchmarkResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Replay & snapshots (R8)
# ---------------------------------------------------------------------------


class ReplayRunState(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ReplayRetrievalParams(BaseModel):
    max_passages: int = Field(ge=1, le=100)
    min_score: float = Field(ge=0.0, le=1.0)


class ReplayRunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    ai_configuration_version_id: str
    retrieval_params: ReplayRetrievalParams
    corpus_snapshot_id: str


class ReplayRunResult(BaseModel):
    answer: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    route: str
    #: Each score in 0.00..1.00.
    retrieval_scores: list[float] = Field(default_factory=list)
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost: float


class ReplayRun(BaseModel):
    replay_run_id: str
    state: ReplayRunState
    request: ReplayRunRequest
    #: Only set when completed.
    result: ReplayRunResult | None = None
    failure_reason: str | None = None
    cancel_requested: bool = False


class CorpusSnapshot(BaseModel):
    corpus_snapshot_id: str
    created_at: str
    #: Immutable manifest of (document_id, document_version) pairs.
    manifest: list[tuple[str, str]] = Field(default_factory=list)


class SqlResultFixture(BaseModel):
    fixture_id: str
    corpus_snapshot_id: str
    sql: str
    #: Key = (corpus_snapshot_id, normalized_sql_hash); lookup on replay (R8.6).
    normalized_sql_hash: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


class SqlResultFixtureInput(BaseModel):
    """Input payload for capturing an SQL result fixture alongside a snapshot."""

    sql: str = Field(min_length=1)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class CreateCorpusSnapshotRequest(BaseModel):
    """Request body for ``POST /corpus-snapshots`` (R8.1, R8.6).

    Captures the current active-version manifest as an immutable CorpusSnapshot.
    An optional ``document_ids`` subset scope restricts which documents are
    included; when omitted all documents with an active version are captured.
    An optional ``sql_fixture`` captures SQL result rows alongside the snapshot.
    """

    #: If provided, only include these documents in the manifest.
    document_ids: list[str] | None = None
    #: Optional SQL result fixture to capture with the snapshot.
    sql_fixture: SqlResultFixtureInput | None = None


class CreateCorpusSnapshotResponse(BaseModel):
    """Response body for ``POST /corpus-snapshots`` — the minted snapshot id."""

    corpus_snapshot_id: str


class CorpusSnapshotSummary(BaseModel):
    """Summary of a CorpusSnapshot for listing (R8.1).

    Contains the snapshot id, creation timestamp, and manifest size (number of
    document/version pairs) so an operator can pick one when initiating a replay.
    """

    corpus_snapshot_id: str
    created_at: str
    manifest_size: int


# ---------------------------------------------------------------------------
# AI configuration (R9)
# ---------------------------------------------------------------------------


class AIConfigurationVersion(BaseModel):
    config_id: str
    version_id: str
    prompt: str
    model: str
    output_schema: dict[str, Any] = Field(default_factory=dict)
    router_threshold: float
    retrieval_settings: dict[str, Any] = Field(default_factory=dict)
    change_description: str = Field(min_length=1, max_length=500)
    created_at: str
    approved: bool = False
    #: Operator who approved (R8.3, R9).
    approver: str | None = None
    #: ISO-8601 UTC approval timestamp.
    approved_at: str | None = None


class ActivationEvent(BaseModel):
    operator: str
    previous_version_id: str | None = None
    selected_version_id: str
    timestamp: str
    reason: str


class AIConfigurationIndex(BaseModel):
    config_id: str
    active_version_id: str | None = None
    #: Version ids, append-only.
    versions: list[str] = Field(default_factory=list)
    activation_events: list[ActivationEvent] = Field(default_factory=list)


class AIConfigCreateRequest(BaseModel):
    """Request body for ``PUT /ai-config/{id}`` (R9.3, R9.4)."""

    prompt: str
    model: str
    router_threshold: float
    change_description: str = Field(min_length=1, max_length=500)
    output_schema: dict[str, Any] | None = None
    retrieval_settings: dict[str, Any] | None = None


class AIConfigRollbackRequest(BaseModel):
    """Request body for ``POST /ai-config/{id}/rollback`` (R9.8, R9.9, R9.10)."""

    version_id: str
    reason: str


# ---------------------------------------------------------------------------
# Trace investigator & knowledge gap (R10, R11)
# ---------------------------------------------------------------------------


class Recommendation(BaseModel):
    target: Literal["ai_configuration", "corpus"]
    description: str


class TraceDiagnosis(BaseModel):
    trace_id: str
    cause_description: str
    analyzed_elements: list[
        Literal["route", "retrieval_scores", "retrieval_order", "generation_outcome"]
    ] = Field(default_factory=list)
    #: 0 when no cause, else 1..10.
    recommendations: list[Recommendation] = Field(default_factory=list)


class KnowledgeGapTopic(BaseModel):
    topic: str
    coverage_quality: Literal["poor", "fair", "good"]
    contributing_question_count: int


class KnowledgeGapMap(BaseModel):
    #: <= configured max.
    topics: list[KnowledgeGapTopic] = Field(default_factory=list)
    recommended_missing_topics: list[str] = Field(default_factory=list)
    documents_needing_reingestion: list[str] = Field(default_factory=list)
    suggested_benchmark_cases: list[str] = Field(default_factory=list)
    frequently_requested_topics: list[str] = Field(default_factory=list)
    eligible_outcome_count: int
    configured_minimum: int


# ---------------------------------------------------------------------------
# Evaluation benchmark case (extends the existing GoldenCase).
#
# Imported here (rather than at module top) to avoid a circular import:
# ``rag_system.evaluation`` imports ``QueryRequest``/``QueryResponse`` from this
# module, both of which are defined above by the time this import runs.
# ---------------------------------------------------------------------------

from rag_system.evaluation import GoldenCase  # noqa: E402


class BenchmarkCase(GoldenCase):
    relevance_labels: RelevanceLabels | None = None
    human_reviewed: bool = False
    #: Free-text expected answer, carried when the case is promoted from a
    #: reviewed Feedback_Item (R6.6).
    expected_answer: str | None = None
