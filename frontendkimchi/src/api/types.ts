// Shared TypeScript shapes mirroring the FastAPI backend contract.
// Treat unknown string values defensively at runtime; do not assume the
// backend only ever returns the documented enum members.

export type DocumentStatus =
  | "queued"
  | "parsing"
  | "chunking"
  | "embedding"
  | "indexed"
  | "failed"
  | "deleted";

export interface DocumentRecord {
  id: string;
  title: string;
  version: string;
  s3_uri: string;
  status: DocumentStatus | string;
  error: string | null;
  /** Authenticated identity that owns the Document (R4.11); null when unknown. */
  owner?: string | null;
  /** Currently active document version (R5); null when none is active yet. */
  active_version?: string | null;
}

export interface BrowserDocumentEntry {
  document: DocumentRecord;
  request_trace_id: string | null;
  added_at: string;
  last_checked_at: string;
}

export interface Citation {
  document_id: string;
  chunk_id: string;
  page_start: number | null;
  page_end: number | null;
  title: string | null;
}

export interface CopilotDataSource {
  table: string;
  columns: string[];
}

export type CopilotRoute = "rag" | "database" | "hybrid" | string;

export interface UnifiedQueryResponse {
  answer: string;
  route: CopilotRoute;
  evidence_status: string;
  trace_id: string;
  citations: Citation[];
  confidence: string | null;
  confidence_score: number | null;
  insufficient_evidence_reason: string | null;
  sql: string | null;
  rows: Record<string, unknown>[];
  data_sources: CopilotDataSource[];
  routing_reasoning: string | null;
  /** Server-side conversation this answer was recorded against. */
  conversation_id: string | null;
  /** The standalone query the follow-up was rewritten into, or null. */
  rewritten_question: string | null;
  /** Claim-level evidence mapping for the answer (R1); empty when none. */
  claims: Claim[];
  /** True when the answer could not be decomposed into claims (R1.9). */
  claim_decomposition_failed: boolean;
}

export interface UnifiedQueryRequest {
  question: string;
  document_ids: string[] | null;
  include_sql: boolean;
  /** Conversation to continue; null starts a new one server-side. */
  conversation_id?: string | null;
  /** Ignore prior turns for this request and clear accumulated context. */
  forget_context?: boolean;
}

export interface ConversationTurn {
  question: string;
  standalone_question: string;
  answer: string;
  route: string;
  trace_id: string;
  asked_at: string;
}

export interface ConversationRecord {
  conversation_id: string;
  created_at: string;
  updated_at: string;
  document_ids: string[] | null;
  turns: ConversationTurn[];
}

export type SpanStatus = "success" | "error";
export type AttributeValue = string | number | boolean;

export interface Span {
  span_id: string;
  parent_span_id: string | null;
  operation: string;
  start_ts: string;
  duration_ms: number;
  status: SpanStatus;
  attributes: Record<string, AttributeValue>;
}

export interface Trace {
  trace_id: string;
  route: string;
  start_ts: string;
  duration_ms: number;
  root_status: SpanStatus;
  spans: Span[];
}

export interface LogRecord {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  trace_id: string | null;
  exc_text: string | null;
  extra: Record<string, AttributeValue>;
  insertion_seq: number;
}

export interface HealthResponse {
  status: string;
}

export interface QueryFeedbackRequest {
  /** 1 (least helpful) – 5 (most helpful). The UI maps 👎→1 and 👍→5. */
  rating: number;
  comment: string | null;
  expected_answer: string | null;
}

export interface QueryFeedbackRecord extends QueryFeedbackRequest {
  trace_id: string;
  feedback_id: string;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Query trace hit (shared by feedback context and trace views)
// ---------------------------------------------------------------------------

export interface QueryTraceHit {
  chunk_id: string;
  document_id: string;
  version: string;
  score: number;
  source: string;
  text: string;
  page_start: number | null;
  page_end: number | null;
  title: string | null;
  section_path: string[];
}

// ---------------------------------------------------------------------------
// Claims and evidence (R1)
// ---------------------------------------------------------------------------

export type VerificationResult = "entails" | "does_not_entail" | "undetermined";

/** How much of a claim a (claim, evidence) pair covers (R1.5). */
export type EvidenceCoverage = "full" | "partial" | "none";

export type EvidenceStatus =
  | "supported"
  | "partially_supported"
  | "unsupported"
  | "verification_unavailable";

export interface AnswerSpan {
  /** Zero-based, inclusive start offset into the answer text. */
  start: number;
  /** Zero-based, exclusive end offset into the answer text; end >= start. */
  end: number;
}

interface EvidenceItemBase {
  verification_result: VerificationResult | string;
  /** Drives partial support (R1.5). */
  coverage: EvidenceCoverage | string;
  /** Optional sub-claim indices this item covers. */
  covered_subclaims: number[];
}

/** Evidence drawn from a document passage. */
export interface DocumentEvidenceItem extends EvidenceItemBase {
  kind: "document";
  quote: string;
  source_start: number;
  source_end: number;
  document_id: string;
  document_version: string;
}

/** Evidence drawn from a database row. */
export interface DatabaseEvidenceItem extends EvidenceItemBase {
  kind: "database";
  table: string;
  row_fields: Record<string, unknown>;
  sql: string | null;
  sql_query_id: string | null;
  /** Nullable for live / unfixtured rows. */
  sql_result_fixture_id: string | null;
  row_index: number | null;
}

/** Discriminated on `kind`; document vs database evidence (R1.2). */
export type EvidenceItem = DocumentEvidenceItem | DatabaseEvidenceItem;

export interface Claim {
  claim_id: string;
  text: string;
  answer_span: AnswerSpan;
  /** 0..100 evidence items associated with this claim. */
  evidence_items: EvidenceItem[];
  evidence_status: EvidenceStatus | string;
}

// ---------------------------------------------------------------------------
// Abstention (R3)
// ---------------------------------------------------------------------------

export type ReasonCode =
  | "low_confidence"
  | "no_evidence"
  | "unsupported_claims"
  | "conflicting_evidence"
  | "sql_no_rows"
  | "retrieval_below_threshold";

/** Returned instead of an answer. Carries no answer/claims/evidence (R3.7). */
export interface AbstentionResponse {
  reason_code: ReasonCode | string;
  /** 1..1000 chars describing the missing information (R3.8). */
  missing_information: string;
  trace_id: string;
}

// ---------------------------------------------------------------------------
// Clarification (R2)
// ---------------------------------------------------------------------------

export interface ClarificationPrompt {
  clarification_question: string;
  clarification_id: string;
  conversation_turn_id: string;
  clarification_expiry: string;
  document_scope: string[] | null;
}

// ---------------------------------------------------------------------------
// Corpus & versions (R4, R5)
// ---------------------------------------------------------------------------

export interface DocumentVersion {
  document_id: string;
  version: string;
  created_at: string;
  indexed: boolean;
  vectors_present: boolean;
  /** All version source content is retained, including superseded (R5.5). */
  source_retained: boolean;
}

export interface IngestionEvent {
  ingestion_id: string;
  document_id: string;
  version: string;
  status: "succeeded" | "failed" | string;
  timestamp: string;
  error: string | null;
}

export interface DocumentVersionIndex {
  document_id: string;
  /** At most one active version per document (R5.4). */
  active_version: string | null;
  versions: DocumentVersion[];
}

/**
 * A document's version history and ingestion events (R5.7).
 * Both lists are ordered by ingestion timestamp, most recent first.
 */
export interface DocumentHistory {
  document_id: string;
  /** The currently active version, or null when nothing is indexed (R5.4). */
  active_version: string | null;
  versions: DocumentVersion[];
  events: IngestionEvent[];
}

export interface CorpusPage {
  documents: DocumentRecord[];
  /** null on the final page (R4.4). */
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// Feedback review (R6)
// ---------------------------------------------------------------------------

export type ReviewStatus = "unreviewed" | "reviewed" | "resolved";

export type FailureCategory =
  | "Missing knowledge"
  | "Retrieval failure"
  | "Wrong route"
  | "Unsupported answer"
  | "SQL problem"
  | "Ambiguous question";

export interface FeedbackReviewRecord extends QueryFeedbackRecord {
  review_status: ReviewStatus | string;
  failure_category: FailureCategory | string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  /** De-dup guard for promotion (R6.11). */
  promoted_case_id: string | null;
}

export interface FeedbackContext {
  feedback: FeedbackReviewRecord;
  expected_answer: string | null;
  confidence: string | null;
  route: string | null;
  retrieved_chunks: QueryTraceHit[];
  sql: string | null;
}

// ---------------------------------------------------------------------------
// Evaluation results (R7)
// ---------------------------------------------------------------------------

export interface RelevanceLabels {
  relevant_chunk_ids: string[];
  relevant_document_ids: string[];
  human_judgments: Record<string, number>;
}

export interface DeterministicCheck {
  name: string;
  outcome: "pass" | "fail" | string;
}

export interface RetrievalMetrics {
  recall_at_k: number;
  precision_at_k: number;
  mrr_at_k: number;
  depth: number;
}

export interface LLMJudgeScores {
  faithfulness: number | null;
  relevance: number | null;
  /** Set on per-case timeout (R7.8). */
  error: string | null;
}

export interface BenchmarkResult {
  case_id: string;
  deterministic_checks: DeterministicCheck[];
  /** null when no relevance labels are present (R7.9). */
  retrieval_metrics: RetrievalMetrics | null;
  llm_judge: LLMJudgeScores | null;
}

// ---------------------------------------------------------------------------
// Replay & snapshots (R8)
// ---------------------------------------------------------------------------

export type ReplayRunState =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface ReplayRetrievalParams {
  max_passages: number;
  min_score: number;
}

export interface ReplayRunRequest {
  question: string;
  ai_configuration_version_id: string;
  retrieval_params: ReplayRetrievalParams;
  corpus_snapshot_id: string;
}

export interface ReplayRunResult {
  answer: string;
  evidence: EvidenceItem[];
  route: string;
  /** Each score in 0.00..1.00. */
  retrieval_scores: number[];
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost: number;
}

export interface ReplayRun {
  replay_run_id: string;
  state: ReplayRunState | string;
  request: ReplayRunRequest;
  /** Only set when completed. */
  result: ReplayRunResult | null;
  failure_reason: string | null;
  cancel_requested: boolean;
}

export interface CorpusSnapshot {
  corpus_snapshot_id: string;
  created_at: string;
  /** Immutable manifest of (document_id, document_version) pairs. */
  manifest: [string, string][];
}

export interface SqlResultFixture {
  fixture_id: string;
  corpus_snapshot_id: string;
  sql: string;
  /** Key = (corpus_snapshot_id, normalized_sql_hash); lookup on replay (R8.6). */
  normalized_sql_hash: string;
  rows: Record<string, unknown>[];
}

// ---------------------------------------------------------------------------
// AI configuration history (R9)
// ---------------------------------------------------------------------------

export interface AIConfigurationVersion {
  config_id: string;
  version_id: string;
  prompt: string;
  model: string;
  output_schema: Record<string, unknown>;
  router_threshold: number;
  retrieval_settings: Record<string, unknown>;
  reranker_config: Record<string, unknown>;
  change_description: string;
  created_at: string;
  approved: boolean;
  /** Operator who approved (R8.3, R9). */
  approver: string | null;
  /** ISO-8601 UTC approval timestamp. */
  approved_at: string | null;
}

export interface ActivationEvent {
  operator: string;
  previous_version_id: string | null;
  selected_version_id: string;
  timestamp: string;
  reason: string;
}

export interface AIConfigurationIndex {
  config_id: string;
  active_version_id: string | null;
  /** Version ids, append-only. */
  versions: string[];
  activation_events: ActivationEvent[];
}

// ---------------------------------------------------------------------------
// Trace investigator & knowledge gap (R10, R11)
// ---------------------------------------------------------------------------

export interface Recommendation {
  target: "ai_configuration" | "corpus" | string;
  description: string;
}

export interface TraceDiagnosis {
  trace_id: string;
  cause_description: string;
  analyzed_elements: (
    | "route"
    | "retrieval_scores"
    | "rerank_order"
    | "generation_outcome"
  )[];
  /** 0 when no cause, else 1..10. */
  recommendations: Recommendation[];
}

export interface KnowledgeGapTopic {
  topic: string;
  coverage_quality: "poor" | "fair" | "good" | string;
  contributing_question_count: number;
}

export interface KnowledgeGapMap {
  /** <= configured max. */
  topics: KnowledgeGapTopic[];
  recommended_missing_topics: string[];
  documents_needing_reingestion: string[];
  suggested_benchmark_cases: string[];
  frequently_requested_topics: string[];
  eligible_outcome_count: number;
  configured_minimum: number;
}
