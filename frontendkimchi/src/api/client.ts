// Typed fetch client. All network access funnels through here so page
// components never call fetch directly.

import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "./tokenStore";

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
  /** Skip attaching the bearer token / 401-refresh (used by the auth endpoints). */
  skipAuth?: boolean;
  /** Internal: set once a request has already been retried after a token refresh. */
  _authRetried?: boolean;
}

// ---------------------------------------------------------------------------
// Token refresh (single-flight). Concurrent 401s share one refresh round-trip
// so we never fire multiple /auth/refresh calls (which would rotate the refresh
// token repeatedly and trip the backend's reuse detection).
// ---------------------------------------------------------------------------

let refreshPromise: Promise<boolean> | null = null;

async function performRefresh(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
  } catch {
    // Network failure — leave the session intact so the user can retry later.
    return false;
  }
  if (!response.ok) {
    // The refresh token is invalid/expired/revoked: end the session so the app
    // redirects to login (subscribers are notified by clearTokens).
    clearTokens();
    return false;
  }
  try {
    const data = await response.json();
    setTokens({ access: data.access_token, refresh: data.refresh_token });
    return true;
  } catch {
    clearTokens();
    return false;
  }
}

/** Refresh the access token, coalescing concurrent callers into one request. */
export function refreshAccessToken(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = performRefresh().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
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
  if (!options.skipAuth) {
    const access = getAccessToken();
    if (access) finalHeaders["Authorization"] = `Bearer ${access}`;
  }

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
        // Access token likely expired — refresh once and retry the request.
        if (
          response.status === 401 &&
          !options.skipAuth &&
          !options._authRetried &&
          getRefreshToken()
        ) {
          const refreshed = await refreshAccessToken();
          if (refreshed) {
            return await request<T>(path, { ...options, _authRetried: true });
          }
        }
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
