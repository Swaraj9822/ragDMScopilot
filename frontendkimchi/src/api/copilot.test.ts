import { describe, expect, it } from "vitest";
import { submitFeedback } from "./copilot";
import { ApiError } from "./client";
import { API, http, HttpResponse, server } from "../test/server";
import type { QueryFeedbackRequest } from "./types";

const TRACE_ID = "b".repeat(32);
const FEEDBACK: QueryFeedbackRequest = { rating: 5, comment: null, expected_answer: null };
// Zero delays keep the retry tests fast and deterministic.
const NO_DELAYS = { retryDelaysMs: [0, 0, 0] };

describe("submitFeedback", () => {
  it("retries a transient 404 (trace not persisted yet) and resolves once it lands", async () => {
    let calls = 0;
    server.use(
      http.post(`${API}/queries/:traceId/feedback`, async ({ request, params }) => {
        calls += 1;
        // The background trace write lands on the third attempt.
        if (calls < 3) {
          return HttpResponse.json({ detail: "Query trace not found." }, { status: 404 });
        }
        const body = (await request.json()) as QueryFeedbackRequest;
        return HttpResponse.json({
          ...body,
          trace_id: String(params.traceId),
          feedback_id: "fb-1",
          created_at: "2026-01-01T00:00:00Z",
        });
      }),
    );

    const record = await submitFeedback(TRACE_ID, FEEDBACK, NO_DELAYS);

    expect(calls).toBe(3);
    expect(record).toMatchObject({ trace_id: TRACE_ID, feedback_id: "fb-1", rating: 5 });
  });

  it("gives up and throws the 404 when the trace never persists", async () => {
    let calls = 0;
    server.use(
      http.post(`${API}/queries/:traceId/feedback`, () => {
        calls += 1;
        return HttpResponse.json({ detail: "Query trace not found." }, { status: 404 });
      }),
    );

    const error = await submitFeedback(TRACE_ID, FEEDBACK, NO_DELAYS).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    // Initial attempt + one per delay in the schedule.
    expect(calls).toBe(NO_DELAYS.retryDelaysMs.length + 1);
  });

  it("does not retry non-404 errors", async () => {
    let calls = 0;
    server.use(
      http.post(`${API}/queries/:traceId/feedback`, () => {
        calls += 1;
        return HttpResponse.json({ detail: "Invalid rating." }, { status: 422 });
      }),
    );

    const error = await submitFeedback(TRACE_ID, FEEDBACK, NO_DELAYS).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect(calls).toBe(1);
  });
});
