import { apiClient, API_BASE_URL, newTraceId, ApiError, NetworkError, TIMEOUT_LONG_MS, TIMEOUT_SHORT_MS, refreshAccessToken } from "./client";
import { getAccessToken } from "./tokenStore";
import type {
  HealthResponse,
  QueryFeedbackRecord,
  QueryFeedbackRequest,
  UnifiedQueryRequest,
  UnifiedQueryResponse,
} from "./types";

export function checkHealth(): Promise<HealthResponse> {
  return apiClient.get<HealthResponse>("/health", { timeoutMs: 15_000 });
}

/**
 * Submit operator feedback for a completed query, keyed by its trace id.
 *
 * The query trace is persisted off the request path, so immediately after an
 * answer the trace may not be written yet and the backend returns 404. Callers
 * should surface that as a "try again in a moment" message rather than a hard
 * error.
 */
export function submitFeedback(
  traceId: string,
  feedback: QueryFeedbackRequest,
): Promise<QueryFeedbackRecord> {
  return apiClient.postJson<QueryFeedbackRecord>(
    `/queries/${encodeURIComponent(traceId)}/feedback`,
    feedback,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
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

// ---------------------------------------------------------------------------
// Streaming (Server-Sent Events) variant of `ask`.
// ---------------------------------------------------------------------------

export interface StreamMeta {
  trace_id: string;
  route: string;
  routing_reasoning: string | null;
}

export interface StreamHandlers {
  /** Routing decision, emitted once before any answer text. */
  onMeta?: (meta: StreamMeta) => void;
  /** Pipeline stage updates: classifying, retrieving, generating, etc. */
  onStatus?: (stage: string) => void;
  /** Incremental answer text. */
  onDelta?: (text: string) => void;
  /** Terminal event with the complete structured response. */
  onFinal?: (response: UnifiedQueryResponse) => void;
  /** Server-side error surfaced mid-stream. */
  onStreamError?: (detail: string) => void;
}

export interface AskStreamResult {
  elapsedMs: number;
  requestTraceId: string;
}

/**
 * POST a question to the streaming endpoint and dispatch parsed SSE events to
 * the provided handlers. Resolves once the stream closes; rejects on transport
 * errors (network/abort). Server-side failures arrive via `onStreamError`.
 */
export async function askStream(
  params: AskParams,
  handlers: StreamHandlers,
): Promise<AskStreamResult> {
  const traceId = newTraceId();
  const payload: UnifiedQueryRequest = {
    question: params.question,
    document_ids: params.documentIds && params.documentIds.length ? params.documentIds : null,
    include_sql: params.includeSql,
  };
  const started = performance.now();

  let response: Response;
  const doFetch = (withSignal: boolean) => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      "X-Trace-Id": traceId,
    };
    const access = getAccessToken();
    if (access) headers["Authorization"] = `Bearer ${access}`;
    return fetch(`${API_BASE_URL}/ask/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      ...(withSignal && params.signal ? { signal: params.signal } : {}),
    });
  };
  try {
    response = await doFetch(true);
  } catch (err) {
    if ((err as Error)?.name === "AbortError") throw err;
    // Some test runtimes (jsdom) reject an AbortSignal at RequestInit
    // validation; fall back to a signal-less request, mirroring api/client.
    if (err instanceof TypeError && /AbortSignal/.test((err as Error).message)) {
      response = await doFetch(false);
    } else {
      throw new NetworkError((err as Error)?.message);
    }
  }

  // Access token may have expired — refresh once and re-open the stream.
  if (response.status === 401) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      response = await doFetch(true).catch(() => doFetch(false));
    }
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // keep generic message
    }
    throw new ApiError(response.status, detail);
  }
  if (!response.body) {
    throw new NetworkError("Streaming is not supported in this environment.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // SSE frames are separated by a blank line. Parse complete frames as they
  // arrive and hold the partial tail for the next read.
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      dispatchSseFrame(frame, handlers);
    }
  }
  if (buffer.trim()) dispatchSseFrame(buffer, handlers);

  return {
    elapsedMs: Math.round(performance.now() - started),
    requestTraceId: traceId,
  };
}

function dispatchSseFrame(frame: string, handlers: StreamHandlers): void {
  let eventType = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) eventType = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return;

  let data: Record<string, unknown>;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return; // ignore malformed frames
  }

  switch (eventType) {
    case "meta":
      handlers.onMeta?.(data as unknown as StreamMeta);
      break;
    case "status":
      handlers.onStatus?.(String(data.stage ?? ""));
      break;
    case "delta":
      handlers.onDelta?.(String(data.text ?? ""));
      break;
    case "final":
      handlers.onFinal?.(data as unknown as UnifiedQueryResponse);
      break;
    case "error":
      handlers.onStreamError?.(String(data.detail ?? "Streaming failed."));
      break;
    default:
      break;
  }
}
