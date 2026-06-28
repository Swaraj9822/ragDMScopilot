import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CopilotPage from "./CopilotPage";
import { renderWithProviders } from "../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../test/server";
import type { UnifiedQueryResponse } from "../api/types";
import { LOCALSTORAGE_KEYS } from "../lib/constants";

function baseResponse(overrides: Partial<UnifiedQueryResponse> = {}): UnifiedQueryResponse {
  return {
    answer: "The total sales were **$1,200**.",
    route: "database",
    evidence_status: "grounded",
    trace_id: "a".repeat(32),
    citations: [],
    confidence: null,
    insufficient_evidence_reason: null,
    sql: "SELECT sum(total) FROM sales;",
    rows: [{ total: 1200 }],
    data_sources: [{ table: "sales", columns: ["total"] }],
    routing_reasoning: "Question is about structured business data.",
    ...overrides,
  };
}

describe("CopilotPage", () => {
  it("sends question, include_sql and selected document ids in the payload", async () => {
    localStorage.setItem(
      LOCALSTORAGE_KEYS.selectedDocuments,
      JSON.stringify({ v: 1, ids: ["doc-123"] }),
    );

    let captured: { question: string; include_sql: boolean; document_ids: string[] | null } | null =
      null;
    server.use(
      http.post(`${API}/ask`, async ({ request }) => {
        captured = (await request.json()) as typeof captured;
        return HttpResponse.json(baseResponse());
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });

    await user.click(screen.getByLabelText(/show generated sql/i));
    await user.type(
      screen.getByLabelText(/ask about your documents/i),
      "What was total sales this month?",
    );
    await user.click(screen.getByRole("button", { name: /send question/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured!.question).toBe("What was total sales this month?");
    expect(captured!.include_sql).toBe(true);
    expect(captured!.document_ids).toEqual(["doc-123"]);
  });

  it("renders a database-route answer with route badge and rows", async () => {
    server.use(http.post(`${API}/ask`, () => HttpResponse.json(baseResponse())));

    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });
    await user.type(screen.getByLabelText(/ask about your documents/i), "sales?");
    await user.click(screen.getByRole("button", { name: /send question/i }));

    expect(await screen.findByText("Database")).toBeInTheDocument();
    expect(screen.getByText(/total sales were/i)).toBeInTheDocument();
  });

  it("renders a hybrid-route answer", async () => {
    server.use(
      http.post(`${API}/ask`, () =>
        HttpResponse.json(baseResponse({ route: "hybrid", answer: "Combined answer." })),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });
    await user.type(screen.getByLabelText(/ask about your documents/i), "compare");
    await user.click(screen.getByRole("button", { name: /send question/i }));
    expect(await screen.findByText("Hybrid")).toBeInTheDocument();
  });

  it("preserves the draft and shows the backend detail on HTTP 400", async () => {
    server.use(
      http.post(`${API}/ask`, () =>
        HttpResponse.json({ detail: "Question is too vague." }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });
    const textarea = screen.getByLabelText(/ask about your documents/i);
    await user.type(textarea, "bad question");
    await user.click(screen.getByRole("button", { name: /send question/i }));

    expect(await screen.findByText(/question is too vague/i)).toBeInTheDocument();
    // The submitted question stays visible in the conversation.
    expect(screen.getByText("bad question")).toBeInTheDocument();
  });
});
