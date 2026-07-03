import { apiClient, API_BASE_URL, newTraceId, ApiError, NetworkError, TIMEOUT_LONG_MS, TIMEOUT_SHORT_MS, refreshAccessToken } from "./client";
import { getAccessToken } from "./tokenStore";
import type {
  AbstentionResponse,
  ClarificationPrompt,
  ConversationRecord,
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
 * Backoff schedule (ms) for retrying feedback submission on a transient 404.
 * The query trace is persisted on a background executor after the answer
 * returns, so for a short window the backend has no trace to attach feedback
 * to. These waits (total ~2.1s) comfortably cover that write landing.
 */
const FEEDBACK_RETRY_DELAYS_MS = [300, 600, 1200];

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Submit operator feedback for a completed query, keyed by its trace id.
 *
 * The query trace is persisted off the request path, so immediately after an
 * answer the trace may not be written yet and the backend returns 404. Rather
 * than push that race onto the user, we retry the submission a few times with
 * backoff; only a 404 that persists past the whole schedule is surfaced (as a
 * "try again in a moment" message) so the operator can retry by hand.
 */
export async function submitFeedback(
  traceId: string,
  feedback: QueryFeedbackRequest,
  options: { retryDelaysMs?: number[] } = {},
): Promise<QueryFeedbackRecord> {
  const delays = options.retryDelaysMs ?? FEEDBACK_RETRY_DELAYS_MS;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await apiClient.postJson<QueryFeedbackRecord>(
        `/queries/${encodeURIComponent(traceId)}/feedback`,
        feedback,
        { timeoutMs: TIMEOUT_SHORT_MS },
      );
    } catch (err) {
      // Only the "trace not written yet" race is retriable. Anything else
      // (validation, auth, server error) propagates immediately.
      if (err instanceof ApiError && err.status === 404 && attempt < delays.length) {
        await delay(delays[attempt]);
        continue;
      }
      throw err;
    }
  }
}

export interface AskParams {
  question: string;
  documentIds: string[] | null;
  includeSql: boolean;
  /** Conversation to continue; null/undefined starts a new one server-side. */
  conversationId?: string | null;
  /** Ignore prior turns for this request and clear accumulated context. */
  forgetContext?: boolean;
  signal?: AbortSignal;
}

function buildAskPayload(params: AskParams): UnifiedQueryRequest {
  return {
    question: params.question,
    document_ids: params.documentIds && params.documentIds.length ? params.documentIds : null,
    include_sql: params.includeSql,
    conversation_id: params.conversationId ?? null,
    forget_context: params.forgetContext ?? false,
  };
}

/** Fetch a stored conversation (history, rewritten queries, document scope). */
export function getConversation(conversationId: string): Promise<ConversationRecord> {
  return apiClient.get<ConversationRecord>(
    `/conversations/${encodeURIComponent(conversationId)}`,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

/**
 * Forget a conversation's accumulated context, preserving its document scope.
 * Subsequent follow-ups stop referencing earlier turns.
 */
export function forgetConversation(conversationId: string): Promise<ConversationRecord> {
  return apiClient.postJson<ConversationRecord>(
    `/conversations/${encodeURIComponent(conversationId)}/forget`,
    {},
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
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
  const payload = buildAskPayload(params);
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
  /** Conversation this turn belongs to (mint-on-first-turn). */
  conversation_id?: string | null;
  /** The standalone rewrite of a follow-up, surfaced for transparency. */
  rewritten_question?: string | null;
}

export interface StreamHandlers {
  /** Routing decision, emitted once before any answer text. */
  onMeta?: (meta: StreamMeta) => void;
  /** Pipeline stage updates: classifying, retrieving, generating, etc. */
  onStatus?: (stage: string) => void;
  /** Incremental answer text. */
  onDelta?: (text: string) => void;
  /** Terminal event carrying the complete structured answer (kind: "answer"). */
  onFinal?: (response: UnifiedQueryResponse) => void;
  /** Terminal event asking for clarification instead of answering (kind: "clarification"). */
  onClarification?: (prompt: ClarificationPrompt) => void;
  /** Terminal event abstaining with no answer content (kind: "abstention"). */
  onAbstention?: (response: AbstentionResponse) => void;
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
  const payload = buildAskPayload(params);
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
      // Legacy/simple shape: the whole frame is the response.
      handlers.onFinal?.(data as unknown as UnifiedQueryResponse);
      break;
    case "terminal": {
      // Held-answer contract (R3.7): exactly one terminal event carrying a
      // `kind` discriminator and the structured payload under `payload`.
      const kind = String(data.kind ?? "");
      const payload = (data.payload ?? {}) as Record<string, unknown>;
      if (kind === "clarification") {
        handlers.onClarification?.(payload as unknown as ClarificationPrompt);
      } else if (kind === "abstention") {
        handlers.onAbstention?.(payload as unknown as AbstentionResponse);
      } else {
        handlers.onFinal?.(payload as unknown as UnifiedQueryResponse);
      }
      break;
    }
    case "error":
      handlers.onStreamError?.(String(data.detail ?? "Streaming failed."));
      break;
    default:
      break;
  }
}
