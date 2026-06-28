// Single-file API client — connects to the FastAPI backend.

const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, public detail: string) { super(detail); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { ...init, headers: { Accept: "application/json", ...init?.headers } });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const b = await res.json(); if (b?.detail) detail = b.detail; } catch { /* noop */ }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ─── Types ───

export interface UnifiedQueryResponse {
  answer: string; route: string; evidence_status: string; trace_id: string;
  citations: { document_id: string; chunk_id: string; page_start: number | null; page_end: number | null; title: string | null }[];
  confidence: string | null; insufficient_evidence_reason: string | null;
  sql: string | null; rows: Record<string, unknown>[]; data_sources: { table: string; columns: string[] }[];
  routing_reasoning: string | null;
}

export interface DocumentRecord {
  id: string; title: string; version: string; s3_uri: string; status: string; error: string | null;
}

export interface Trace {
  trace_id: string; route: string; start_ts: string; duration_ms: number; root_status: string;
  spans: Span[];
}

export interface Span {
  span_id: string; parent_span_id: string | null; operation: string;
  start_ts: string; duration_ms: number; status: string;
  attributes: Record<string, string | number | boolean>;
}

export interface LogEntry {
  timestamp: string; level: string; logger: string; message: string;
  trace_id: string | null; exc_text: string | null; extra: Record<string, unknown>; insertion_seq: number;
}

// ─── Endpoints ───

export const api = {
  health: () => request<{ status: string }>("/health"),

  ask: (question: string, opts?: { documentIds?: string[]; includeSql?: boolean; signal?: AbortSignal }) =>
    request<UnifiedQueryResponse>("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, document_ids: opts?.documentIds ?? null, include_sql: opts?.includeSql ?? false }),
      signal: opts?.signal,
    }),

  uploadDocument: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<DocumentRecord>("/documents", { method: "POST", body: form });
  },

  getDocument: (id: string) => request<DocumentRecord>(`/documents/${encodeURIComponent(id)}`),
  deleteDocument: (id: string) => request<DocumentRecord>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),

  searchTraces: (params?: { start?: string; end?: string; route?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.start) qs.set("start", params.start);
    if (params?.end) qs.set("end", params.end);
    if (params?.route) qs.set("route", params.route);
    if (params?.status) qs.set("status", params.status);
    if (params?.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return request<Trace[]>(`/traces${q ? `?${q}` : ""}`);
  },

  getTrace: (id: string) => request<Trace>(`/traces/${encodeURIComponent(id)}`),

  searchLogs: (params?: { start?: string; end?: string; level?: string; trace_id?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.start) qs.set("start", params.start);
    if (params?.end) qs.set("end", params.end);
    if (params?.level) qs.set("level", params.level);
    if (params?.trace_id) qs.set("trace_id", params.trace_id);
    if (params?.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return request<LogEntry[]>(`/logs${q ? `?${q}` : ""}`);
  },

  getLogsByTrace: (traceId: string) => request<LogEntry[]>(`/logs/${encodeURIComponent(traceId)}`),
};
