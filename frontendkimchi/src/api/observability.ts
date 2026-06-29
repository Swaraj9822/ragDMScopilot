import { apiClient, TIMEOUT_SHORT_MS } from "./client";
import type { LogRecord, Trace } from "./types";

export interface TraceSearchParams {
  start?: string | null;
  end?: string | null;
  route?: string | null;
  status?: "success" | "error" | null;
  minDurationMs?: number | null;
  limit?: number | null;
}

function buildQuery(entries: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(entries)) {
    if (value !== null && value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export function searchTraces(params: TraceSearchParams): Promise<Trace[]> {
  const query = buildQuery({
    start: params.start,
    end: params.end,
    route: params.route,
    status: params.status,
    min_duration_ms: params.minDurationMs,
    limit: params.limit,
  });
  return apiClient.get<Trace[]>(`/traces${query}`, { timeoutMs: TIMEOUT_SHORT_MS });
}

export function getTrace(traceId: string): Promise<Trace> {
  return apiClient.get<Trace>(`/traces/${encodeURIComponent(traceId)}`, {
    timeoutMs: TIMEOUT_SHORT_MS,
  });
}

export interface LogSearchParams {
  start?: string | null;
  end?: string | null;
  level?: string | null;
  traceId?: string | null;
  limit?: number | null;
}

export function searchLogs(params: LogSearchParams): Promise<LogRecord[]> {
  const query = buildQuery({
    start: params.start,
    end: params.end,
    level: params.level,
    trace_id: params.traceId,
    limit: params.limit,
  });
  return apiClient.get<LogRecord[]>(`/logs${query}`, { timeoutMs: TIMEOUT_SHORT_MS });
}

export function getLogsByTrace(traceId: string): Promise<LogRecord[]> {
  return apiClient.get<LogRecord[]>(`/logs/${encodeURIComponent(traceId)}`, {
    timeoutMs: TIMEOUT_SHORT_MS,
  });
}
