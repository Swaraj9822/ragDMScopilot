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
  insufficient_evidence_reason: string | null;
  sql: string | null;
  rows: Record<string, unknown>[];
  data_sources: CopilotDataSource[];
  routing_reasoning: string | null;
}

export interface UnifiedQueryRequest {
  question: string;
  document_ids: string[] | null;
  include_sql: boolean;
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
