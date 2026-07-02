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
    confidence_score: 0.86,
    insufficient_evidence_reason: null,
    sql: "SELECT sum(total) FROM sales;",
    rows: [{ total: 1200 }],
    data_sources: [{ table: "sales", columns: ["total"] }],
    routing_reasoning: "Question is about structured business data.",
    ...overrides,
  };
}

/** Build a streaming SSE Response from a list of {event, data} frames. */
function sseResponse(frames: Array<{ event: string; data: unknown }>) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(
          encoder.encode(`event: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`),
        );
      }
      controller.close();
    },
  });
  return new HttpResponse(stream, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Default streaming handler that echoes a response as meta + delta + final. */
function streamHandler(response: UnifiedQueryResponse) {
  return sseResponse([
    { event: "meta", data: { trace_id: response.trace_id, route: response.route, routing_reasoning: response.routing_reasoning } },
    { event: "delta", data: { text: response.answer } },
    { event: "final", data: response },
  ]);
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
      http.post(`${API}/ask/stream`, async ({ request }) => {
        captured = (await request.json()) as typeof captured;
        return streamHandler(baseResponse());
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
    server.use(http.post(`${API}/ask/stream`, () => streamHandler(baseResponse())));

    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });
    await user.type(screen.getByLabelText(/ask about your documents/i), "sales?");
    await user.click(screen.getByRole("button", { name: /send question/i }));

    expect(await screen.findByText("Database")).toBeInTheDocument();
    expect(screen.getByText(/total sales were/i)).toBeInTheDocument();
  });

  it("renders a hybrid-route answer", async () => {
    server.use(
      http.post(`${API}/ask/stream`, () =>
        streamHandler(baseResponse({ route: "hybrid", answer: "Combined answer." })),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });
    await user.type(screen.getByLabelText(/ask about your documents/i), "compare");
    await user.click(screen.getByRole("button", { name: /send question/i }));
    expect(await screen.findByText("Hybrid")).toBeInTheDocument();
  });

  it("surfaces a server-sent error event mid-stream", async () => {
    server.use(
      http.post(`${API}/ask/stream`, () =>
        sseResponse([
          { event: "meta", data: { trace_id: "b".repeat(32), route: "rag", routing_reasoning: null } },
          { event: "error", data: { detail: "Question is too vague." } },
        ]),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });
    await user.type(screen.getByLabelText(/ask about your documents/i), "bad question");
    await user.click(screen.getByRole("button", { name: /send question/i }));

    expect(await screen.findByText(/question is too vague/i)).toBeInTheDocument();
    // The submitted question stays visible in the conversation.
    expect(screen.getByText("bad question")).toBeInTheDocument();
  });

  it("clears the selected documents via New session so later requests are unconstrained", async () => {
    localStorage.setItem(
      LOCALSTORAGE_KEYS.selectedDocuments,
      JSON.stringify({ v: 1, ids: ["doc-123"] }),
    );

    let captured: { document_ids: string[] | null } | null = null;
    server.use(
      http.post(`${API}/ask/stream`, async ({ request }) => {
        captured = (await request.json()) as typeof captured;
        return streamHandler(baseResponse());
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });

    // The active document constraint is visible in the context rail.
    expect(
      screen.getByRole("button", { name: /remove document doc-123/i }),
    ).toBeInTheDocument();

    // Start a new session and confirm — this clears the selection too.
    await user.click(screen.getByRole("button", { name: /new session/i }));
    await user.click(screen.getByRole("button", { name: /clear session/i }));

    expect(
      screen.queryByRole("button", { name: /remove document doc-123/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/no documents selected/i)).toBeInTheDocument();

    // A subsequent question is no longer constrained to the old selection.
    await user.type(screen.getByLabelText(/ask about your documents/i), "anything?");
    await user.click(screen.getByRole("button", { name: /send question/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured!.document_ids ?? []).toHaveLength(0);
  });

  it("removes a single selected document from the context rail", async () => {
    localStorage.setItem(
      LOCALSTORAGE_KEYS.selectedDocuments,
      JSON.stringify({ v: 1, ids: ["doc-1", "doc-2"] }),
    );

    const user = userEvent.setup();
    renderWithProviders(<CopilotPage />, { route: "/copilot" });

    await user.click(screen.getByRole("button", { name: /remove document doc-1/i }));

    expect(
      screen.queryByRole("button", { name: /remove document doc-1/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /remove document doc-2/i }),
    ).toBeInTheDocument();
  });

  it("preserves the draft and shows the backend detail on HTTP 400", async () => {
    server.use(
      http.post(`${API}/ask/stream`, () =>
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
