import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AnswerCard } from "./AnswerCard";
import { renderWithProviders } from "../../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../../test/server";
import type { QueryFeedbackRequest, UnifiedQueryResponse } from "../../api/types";

const TRACE_ID = "a".repeat(32);

function baseResponse(overrides: Partial<UnifiedQueryResponse> = {}): UnifiedQueryResponse {
  return {
    answer: "The total sales were **$1,200**.",
    route: "database",
    evidence_status: "grounded",
    trace_id: TRACE_ID,
    citations: [],
    confidence: null,
    confidence_score: 0.86,
    insufficient_evidence_reason: null,
    sql: null,
    rows: [],
    data_sources: [],
    routing_reasoning: null,
    ...overrides,
  };
}

/**
 * Register a feedback handler that captures the posted body and echoes back a
 * feedback record. Returns a getter for the captured request payload.
 */
function captureFeedback(status = 200) {
  const captured: { body: QueryFeedbackRequest | null; traceId: string | null } = {
    body: null,
    traceId: null,
  };
  server.use(
    http.post(`${API}/queries/:traceId/feedback`, async ({ request, params }) => {
      captured.traceId = String(params.traceId);
      if (status !== 200) {
        return HttpResponse.json({ detail: "Query trace not found." }, { status });
      }
      captured.body = (await request.json()) as QueryFeedbackRequest;
      return HttpResponse.json({
        ...captured.body,
        trace_id: captured.traceId,
        feedback_id: "fb-1",
        created_at: "2026-01-01T00:00:00Z",
      });
    }),
  );
  return captured;
}

describe("AnswerCard feedback", () => {
  it("submits rating 5 with no detail when the user clicks Helpful", async () => {
    const captured = captureFeedback();
    const user = userEvent.setup();
    renderWithProviders(<AnswerCard response={baseResponse()} elapsedMs={1200} />);

    await user.click(screen.getByRole("button", { name: /^helpful$/i }));

    await waitFor(() => expect(captured.body).not.toBeNull());
    expect(captured.traceId).toBe(TRACE_ID);
    expect(captured.body).toEqual({ rating: 5, comment: null, expected_answer: null });
    // Inline confirmation replaces the prompt.
    expect(await screen.findByText(/thanks for the feedback/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^helpful$/i })).not.toBeInTheDocument();
  });

  it("reveals the detail form on Not helpful and posts rating 1 with comment and expected answer", async () => {
    const captured = captureFeedback();
    const user = userEvent.setup();
    renderWithProviders(<AnswerCard response={baseResponse()} elapsedMs={900} />);

    // Clicking Not helpful should not submit yet — it opens the optional form.
    await user.click(screen.getByRole("button", { name: /not helpful/i }));
    expect(captured.body).toBeNull();

    await user.type(
      screen.getByPlaceholderText(/missing context, wrong citation/i),
      "Cited the wrong document.",
    );
    await user.type(
      screen.getByPlaceholderText(/what should the answer have said/i),
      "Total was $2,400.",
    );
    await user.click(screen.getByRole("button", { name: /send feedback/i }));

    await waitFor(() => expect(captured.body).not.toBeNull());
    expect(captured.body).toEqual({
      rating: 1,
      comment: "Cited the wrong document.",
      expected_answer: "Total was $2,400.",
    });
    expect(await screen.findByText(/thanks for the feedback/i)).toBeInTheDocument();
  });

  it("keeps the prompt after the auto-retries are exhausted (persistent 404) so the user can retry", async () => {
    // submitFeedback auto-retries a 404 with backoff before giving up, so allow
    // extra time for the full schedule to elapse.
    captureFeedback(404);
    const user = userEvent.setup();
    renderWithProviders(<AnswerCard response={baseResponse()} elapsedMs={800} />);

    const helpful = screen.getByRole("button", { name: /^helpful$/i });
    await user.click(helpful);

    // The control is disabled while retries are in flight, then re-enables once
    // they're exhausted — no success confirmation, so the operator can retry.
    await waitFor(() => expect(helpful).toBeDisabled());
    await waitFor(() => expect(helpful).toBeEnabled(), { timeout: 8000 });
    expect(screen.queryByText(/thanks for the feedback/i)).not.toBeInTheDocument();
  }, 10_000);

  it("omits the feedback control when the answer has no trace id", () => {
    renderWithProviders(
      <AnswerCard response={baseResponse({ trace_id: "" })} elapsedMs={500} />,
    );
    expect(screen.queryByText(/was this answer helpful/i)).not.toBeInTheDocument();
  });
});
