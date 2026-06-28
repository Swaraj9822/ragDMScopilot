// Typed fetch client. All network access funnels through here so page
// components never call fetch directly.

export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

export const TIMEOUT_LONG_MS = 120_000; // AI queries + uploads
export const TIMEOUT_SHORT_MS = 15_000; // health / search / detail

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export class NetworkError extends Error {
  constructor(message = "Network request failed") {
    super(message);
    this.name = "NetworkError";
  }
}

export class TimeoutError extends Error {
  constructor(message = "Request timed out") {
    super(message);
    this.name = "TimeoutError";
  }
}

/** Generate a 32-char lowercase hex trace id for X-Trace-Id headers. */
export function newTraceId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID().replaceAll("-", "");
  }
  // Fallback for very old environments.
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

async function parseError(response: Response): Promise<ApiError> {
  let detail = `Request failed with status ${response.status}`;
  try {
    const body = await response.json();
    if (body && typeof body.detail === "string") {
      detail = body.detail;
    } else if (typeof body?.detail !== "undefined") {
      detail = JSON.stringify(body.detail);
    }
  } catch {
    // Non-JSON error body; keep generic message.
  }
  return new ApiError(response.status, detail);
}

interface RequestOptions {
  method?: string;
  body?: BodyInit | null;
  headers?: Record<string, string>;
  timeoutMs?: number;
  traceId?: string;
  signal?: AbortSignal;
  /** Retry idempotent GET requests up to this many times. */
  retries?: number;
}

async function rawRequest(
  path: string,
  options: RequestOptions,
): Promise<Response> {
  const {
    method = "GET",
    body,
    headers = {},
    timeoutMs = TIMEOUT_SHORT_MS,
    traceId,
    signal,
  } = options;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new TimeoutError()), timeoutMs);

  // Bridge a caller-provided signal into our controller.
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener("abort", () => controller.abort(signal.reason));
  }

  const finalHeaders: Record<string, string> = {
    Accept: "application/json",
    ...headers,
  };
  if (traceId) finalHeaders["X-Trace-Id"] = traceId;

  try {
    return await fetch(`${API_BASE_URL}${path}`, {
      method,
      body,
      headers: finalHeaders,
      signal: controller.signal,
    });
  } catch (err) {
    if (controller.signal.aborted && controller.signal.reason instanceof TimeoutError) {
      throw controller.signal.reason;
    }
    if (signal?.aborted) throw err;
    // Some test runtimes (jsdom) expose an AbortSignal that Node's fetch
    // rejects at RequestInit validation. Real browsers never hit this; fall
    // back to a signal-less request so behavior degrades gracefully.
    if (err instanceof TypeError && /AbortSignal/.test(err.message)) {
      return await fetch(`${API_BASE_URL}${path}`, {
        method,
        body,
        headers: finalHeaders,
      });
    }
    throw new NetworkError((err as Error)?.message);
  } finally {
    clearTimeout(timeout);
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const retries = options.method && options.method !== "GET" ? 0 : options.retries ?? 2;

  let attempt = 0;
  // Only GET requests are retried, and only on network/5xx failures.
  for (;;) {
    try {
      const response = await rawRequest(path, options);
      if (!response.ok) {
        const error = await parseError(response);
        if (retries > attempt && response.status >= 500 && (!options.method || options.method === "GET")) {
          attempt += 1;
          await delay(2 ** attempt * 150);
          continue;
        }
        throw error;
      }
      if (response.status === 204) return undefined as T;
      const text = await response.text();
      return (text ? JSON.parse(text) : undefined) as T;
    } catch (err) {
      const retriable = err instanceof NetworkError || err instanceof TimeoutError;
      if (retriable && retries > attempt && (!options.method || options.method === "GET")) {
        attempt += 1;
        await delay(2 ** attempt * 150);
        continue;
      }
      throw err;
    }
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const apiClient = {
  get: <T>(path: string, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "GET" }),
  postJson: <T>(path: string, data: unknown, opts?: RequestOptions) =>
    request<T>(path, {
      ...opts,
      method: "POST",
      body: JSON.stringify(data),
      headers: { ...opts?.headers, "Content-Type": "application/json" },
    }),
  // For FormData we deliberately do not set Content-Type so the browser adds
  // the multipart boundary.
  sendForm: <T>(path: string, method: "POST" | "PUT", form: FormData, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method, body: form }),
  delete: <T>(path: string, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "DELETE" }),
};
