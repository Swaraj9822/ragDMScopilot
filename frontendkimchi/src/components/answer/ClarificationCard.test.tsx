import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ClarificationCard } from "./ClarificationCard";
import type { ClarificationResult } from "./ClarificationCard";
import { renderWithProviders } from "../../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../../test/server";
import type { ClarificationPrompt, UnifiedQueryResponse } from "../../api/types";

function basePrompt(overrides: Partial<ClarificationPrompt> = {}): ClarificationPrompt {
  return {
    clarification_question: "Did you mean sales for Q1 2024 or Q2 2024?",
    clarification_id: "clar_abc123",
    conversation_turn_id: "turn-1",
    clarification_expiry: "2099-12-31T23:59:59Z",
    document_scope: null,
    ...overrides,
  };
}

function mockClarifySuccess(response: Partial<UnifiedQueryResponse> = {}) {
  server.use(
    http.post(`${API}/ask/clarify`, () =>
      HttpResponse.json({
        answer: "Total sales for Q1 2024 were $1,200.",
        route: "rag",
        evidence_status: "grounded",
        trace_id: "t".repeat(32),
        citations: [],
        confidence: null,
        confidence_score: 0.9,
        insufficient_evidence_reason: null,
        sql: null,
        rows: [],
        data_sources: [],
        routing_reasoning: null,
        conversation_id: null,
        rewritten_question: null,
        claims: [],
        claim_decomposition_failed: false,
        ...response,
      }),
    ),
  );
}

function mockClarifyAbstention() {
  server.use(
    http.post(`${API}/ask/clarify`, () =>
      HttpResponse.json({
        reason_code: "no_evidence",
        missing_information: "No documents cover Q3 2024 sales data.",
        trace_id: "t".repeat(32),
      }),
    ),
  );
}

function mockClarifyError(status: number, detail: string) {
  server.use(
    http.post(`${API}/ask/clarify`, () =>
      HttpResponse.json({ detail }, { status }),
    ),
  );
}

describe("ClarificationCard", () => {
  it("renders the clarification question", () => {
    renderWithProviders(<ClarificationCard prompt={basePrompt()} />);

    expect(
      screen.getByText("Did you mean sales for Q1 2024 or Q2 2024?"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Your reply"),
    ).toBeInTheDocument();
  });

  it("disables the submit button when the reply is empty", () => {
    renderWithProviders(<ClarificationCard prompt={basePrompt()} />);

    const submitBtn = screen.getByRole("button", { name: /reply/i });
    expect(submitBtn).toBeDisabled();
  });

  it("submits the reply to /ask/clarify and calls onResult with an answer", async () => {
    mockClarifySuccess();
    const onResult = vi.fn();
    const user = userEvent.setup();

    renderWithProviders(
      <ClarificationCard prompt={basePrompt()} onResult={onResult} />,
    );

    await user.type(screen.getByLabelText("Your reply"), "Q1 2024");
    await user.click(screen.getByRole("button", { name: /reply/i }));

    await waitFor(() => expect(onResult).toHaveBeenCalledTimes(1));
    const result: ClarificationResult = onResult.mock.calls[0][0];
    expect(result.kind).toBe("answer");
    if (result.kind === "answer") {
      expect(result.response.answer).toContain("Q1 2024");
    }
  });

  it("submits and calls onResult with an abstention when the backend abstains", async () => {
    mockClarifyAbstention();
    const onResult = vi.fn();
    const user = userEvent.setup();

    renderWithProviders(
      <ClarificationCard prompt={basePrompt()} onResult={onResult} />,
    );

    await user.type(screen.getByLabelText("Your reply"), "Q3 2024");
    await user.click(screen.getByRole("button", { name: /reply/i }));

    await waitFor(() => expect(onResult).toHaveBeenCalledTimes(1));
    const result: ClarificationResult = onResult.mock.calls[0][0];
    expect(result.kind).toBe("abstention");
    if (result.kind === "abstention") {
      expect(result.response.reason_code).toBe("no_evidence");
    }
  });

  it("shows an inline error when the clarification is expired/invalid", async () => {
    mockClarifyError(400, "clarification_invalid_or_expired");
    const onResult = vi.fn();
    const user = userEvent.setup();

    renderWithProviders(
      <ClarificationCard prompt={basePrompt()} onResult={onResult} />,
    );

    await user.type(screen.getByLabelText("Your reply"), "Q1 2024");
    await user.click(screen.getByRole("button", { name: /reply/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/expired or is no longer valid/i);
    // onResult should NOT be called on error
    expect(onResult).not.toHaveBeenCalled();
  });

  it("shows an inline error when reply_required is returned", async () => {
    mockClarifyError(400, "clarification_reply_required");
    const onResult = vi.fn();
    const user = userEvent.setup();

    renderWithProviders(
      <ClarificationCard prompt={basePrompt()} onResult={onResult} />,
    );

    await user.type(screen.getByLabelText("Your reply"), "  hello  ");
    await user.click(screen.getByRole("button", { name: /reply/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/reply is required/i);
    expect(onResult).not.toHaveBeenCalled();
  });

  it("shows document scope info when present", () => {
    renderWithProviders(
      <ClarificationCard
        prompt={basePrompt({ document_scope: ["doc-1", "doc-2"] })}
      />,
    );

    expect(screen.getByText(/scoped to 2 documents/i)).toBeInTheDocument();
  });

  it("does not show document scope info when scope is null", () => {
    renderWithProviders(
      <ClarificationCard prompt={basePrompt({ document_scope: null })} />,
    );

    expect(screen.queryByText(/scoped to/i)).not.toBeInTheDocument();
  });

  it("re-enables the form after an error so the user can retry", async () => {
    mockClarifyError(400, "clarification_invalid_or_expired");
    const user = userEvent.setup();

    renderWithProviders(<ClarificationCard prompt={basePrompt()} />);

    await user.type(screen.getByLabelText("Your reply"), "Q1 2024");
    await user.click(screen.getByRole("button", { name: /reply/i }));

    // Wait for error display
    await screen.findByRole("alert");

    // Form should be enabled again for retry
    const textarea = screen.getByLabelText("Your reply");
    expect(textarea).not.toBeDisabled();
    const submitBtn = screen.getByRole("button", { name: /reply/i });
    expect(submitBtn).not.toBeDisabled();
  });
});
