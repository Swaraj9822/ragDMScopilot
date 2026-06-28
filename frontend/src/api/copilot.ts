import { apiClient, newTraceId, TIMEOUT_LONG_MS } from "./client";
import type { HealthResponse, UnifiedQueryRequest, UnifiedQueryResponse } from "./types";

export function checkHealth(): Promise<HealthResponse> {
  return apiClient.get<HealthResponse>("/health", { timeoutMs: 15_000 });
}

export interface AskParams {
  question: string;
  documentIds: string[] | null;
  includeSql: boolean;
  signal?: AbortSignal;
}

export interface AskResult {
  response: UnifiedQueryResponse;
  /** Client-measured round-trip time in milliseconds. */
  elapsedMs: number;
  /** The trace id we forced via X-Trace-Id (matches response.trace_id). */
  requestTraceId: string;
}

export async function ask(params: AskParams): Promise<AskResult> {
  const traceId = newTraceId();
  const payload: UnifiedQueryRequest = {
    question: params.question,
    document_ids: params.documentIds && params.documentIds.length ? params.documentIds : null,
    include_sql: params.includeSql,
  };
  const started = performance.now();
  const response = await apiClient.postJson<UnifiedQueryResponse>("/ask", payload, {
    timeoutMs: TIMEOUT_LONG_MS,
    traceId,
    signal: params.signal,
  });
  return {
    response,
    elapsedMs: Math.round(performance.now() - started),
    requestTraceId: traceId,
  };
}
