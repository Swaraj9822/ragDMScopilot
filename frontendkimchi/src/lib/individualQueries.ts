import type { AttributeValue, Span, Trace } from "../api/types";

// HTTP routes whose traces represent a user "ask" — RAG, database copilot, or
// the unified router (streaming and non-streaming). Each carries a "query
// summary" span with the question, confidence score, and total token usage.
const QUERY_ROUTES = new Set(["/ask", "/ask/stream", "/query", "/copilot/query"]);

// The ingestion worker opens a trace under this route around the entire
// parse → chunk → embed → index job, so its duration is the full time to
// finish and its spans carry the source filename. (The fast POST /documents
// enqueue trace is intentionally ignored — it is not the job latency.)
const UPLOAD_ROUTE = "ingestion";

// Sentinel the backend records when an expected attribute value is missing.
const UNAVAILABLE = "unavailable";

export type IndividualEntryKind = "query" | "upload";

export interface IndividualEntry {
  traceId: string;
  kind: IndividualEntryKind;
  startTs: string;
  durationMs: number;
  status: "success" | "error";
  /** Query rows only. */
  question: string | null;
  confidenceScore: number | null;
  totalTokens: number | null;
  /** Upload rows only. */
  filename: string | null;
}

function asNumber(value: AttributeValue | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asText(value: AttributeValue | undefined): string | null {
  return typeof value === "string" && value && value !== UNAVAILABLE ? value : null;
}

function findSummarySpan(trace: Trace): Span | undefined {
  return trace.spans.find(
    (span) => span.attributes?.summary_kind === "query" || span.operation === "query summary",
  );
}

/** Sum token usage across every span that recorded a numeric total. */
function sumSpanTokens(trace: Trace): number | null {
  let total = 0;
  for (const span of trace.spans) {
    const tokens = asNumber(span.attributes?.total_tokens);
    if (tokens !== null) total += tokens;
  }
  return total > 0 ? total : null;
}

/**
 * Transform raw traces into the two row types shown in the Individual Query
 * tab: one per ask query and one per finished upload. Traces that are neither
 * (console traffic, health checks, the /documents enqueue, etc.) are dropped.
 * The result is sorted newest first.
 */
export function buildIndividualEntries(traces: Trace[]): IndividualEntry[] {
  const entries: IndividualEntry[] = [];

  for (const trace of traces) {
    if (QUERY_ROUTES.has(trace.route)) {
      const attrs = findSummarySpan(trace)?.attributes ?? {};
      entries.push({
        traceId: trace.trace_id,
        kind: "query",
        startTs: trace.start_ts,
        durationMs: trace.duration_ms,
        status: trace.root_status,
        question: asText(attrs.question),
        confidenceScore: asNumber(attrs.confidence_score),
        totalTokens: asNumber(attrs.total_tokens) ?? sumSpanTokens(trace),
        filename: null,
      });
    } else if (trace.route === UPLOAD_ROUTE) {
      const fileSpan = trace.spans.find((span) => asText(span.attributes?.source_filename));
      entries.push({
        traceId: trace.trace_id,
        kind: "upload",
        startTs: trace.start_ts,
        durationMs: trace.duration_ms,
        status: trace.root_status,
        question: null,
        confidenceScore: null,
        totalTokens: null,
        filename: fileSpan ? asText(fileSpan.attributes.source_filename) : null,
      });
    }
  }

  entries.sort((a, b) => new Date(b.startTs).getTime() - new Date(a.startTs).getTime());
  return entries;
}

/** Coarse confidence band for colour coding. Mirrors the backend bands. */
export function confidenceBand(score: number | null): "high" | "medium" | "low" | null {
  if (score === null) return null;
  if (score >= 0.7) return "high";
  if (score >= 0.4) return "medium";
  return "low";
}
