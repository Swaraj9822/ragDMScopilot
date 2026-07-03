import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FeedbackInboxPage from "./FeedbackInboxPage";
import { renderWithProviders } from "../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../test/server";
import type { FeedbackContext } from "../api/types";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeFeedbackItem(overrides: Partial<FeedbackContext> = {}): FeedbackContext {
  return {
    feedback: {
      trace_id: "trace-1",
      feedback_id: "fb-1",
      created_at: "2025-06-01T12:00:00Z",
      rating: 1,
      comment: "This answer was wrong",
      expected_answer: "The correct answer is X",
      review_status: "unreviewed",
      failure_category: null,
      reviewed_by: null,
      reviewed_at: null,
      promoted_case_id: null,
      ...(overrides.feedback ?? {}),
    },
    expected_answer: "The correct answer is X",
    confidence: "0.72",
    route: "rag",
    retrieved_chunks: [
      {
        chunk_id: "chunk-a",
        document_id: "doc-1",
        version: "v1",
        score: 0.89,
        source: "source.pdf",
        text: "Some relevant passage text here",
        page_start: 1,
        page_end: 2,
        title: "Source Document",
        section_path: [],
      },
    ],
    sql: null,
    ...overrides,
  };
}

function makePage(items: FeedbackContext[], nextCursor: string | null = null) {
  return { items, next_cursor: nextCursor };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("FeedbackInboxPage", () => {
  it("renders feedback items with full context (R6.9)", async () => {
    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(makePage([makeFeedbackItem()])),
      ),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    // Wait for data to load
    expect(await screen.findByText("Rating 1")).toBeInTheDocument();
    expect(screen.getByText("This answer was wrong")).toBeInTheDocument();
    expect(screen.getByText("The correct answer is X")).toBeInTheDocument();
    expect(screen.getByText("0.72")).toBeInTheDocument();
    // Route badge renders "rag" → "Document"
    expect(screen.getByText("Document")).toBeInTheDocument();
    // Review status shown
    expect(screen.getAllByText("unreviewed").length).toBeGreaterThanOrEqual(1);
  });

  it("shows empty state when no feedback items exist", async () => {
    server.use(
      http.get(`${API}/feedback`, () => HttpResponse.json(makePage([]))),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    expect(await screen.findByText("No feedback items")).toBeInTheDocument();
    expect(screen.getByText(/no negative-rating feedback/i)).toBeInTheDocument();
  });

  it("filters by review_status", async () => {
    const user = userEvent.setup();
    let requestedStatus: string | null = null;

    server.use(
      http.get(`${API}/feedback`, ({ request }) => {
        const url = new URL(request.url);
        requestedStatus = url.searchParams.get("review_status");
        if (requestedStatus === "resolved") {
          return HttpResponse.json(
            makePage([
              makeFeedbackItem({
                feedback: {
                  trace_id: "trace-2",
                  feedback_id: "fb-2",
                  created_at: "2025-06-02T12:00:00Z",
                  rating: 2,
                  comment: "Resolved comment",
                  expected_answer: null,
                  review_status: "resolved",
                  failure_category: "Wrong route",
                  reviewed_by: "operator@test.com",
                  reviewed_at: "2025-06-02T13:00:00Z",
                  promoted_case_id: null,
                },
              }),
            ]),
          );
        }
        return HttpResponse.json(makePage([makeFeedbackItem()]));
      }),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    // Wait for initial load
    await screen.findByText("Rating 1");

    // Select "Resolved" filter
    const filterSelect = screen.getByLabelText("Filter by review status");
    await user.selectOptions(filterSelect, "resolved");

    // Should show the resolved item
    await screen.findByText("Resolved comment");
    expect(requestedStatus).toBe("resolved");
  });

  it("provides classify action with six categories", async () => {
    const user = userEvent.setup();
    let classifiedCategory: string | null = null;

    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(makePage([makeFeedbackItem()])),
      ),
      http.post(`${API}/feedback/:id/classify`, async ({ request }) => {
        const body = (await request.json()) as { category: string };
        classifiedCategory = body.category;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    await screen.findByText("Rating 1");

    const classifySelect = screen.getByLabelText("Classify feedback");
    await user.selectOptions(classifySelect, "Wrong route");

    await waitFor(() => expect(classifiedCategory).toBe("Wrong route"));
  });

  it("provides resolve action", async () => {
    const user = userEvent.setup();
    let resolved = false;

    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(makePage([makeFeedbackItem()])),
      ),
      http.post(`${API}/feedback/:id/resolve`, () => {
        resolved = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    await screen.findByText("Rating 1");

    await user.click(screen.getByLabelText("Resolve feedback"));

    await waitFor(() => expect(resolved).toBe(true));
  });

  it("removes a resolved item from a filtered view it no longer matches", async () => {
    const user = userEvent.setup();

    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(makePage([makeFeedbackItem()])),
      ),
      http.post(`${API}/feedback/:id/resolve`, () =>
        new HttpResponse(null, { status: 204 }),
      ),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    await screen.findByText("Rating 1");

    // Constrain the view to unreviewed items (the item is unreviewed).
    await user.selectOptions(
      screen.getByLabelText("Filter by review status"),
      "unreviewed",
    );
    expect(await screen.findByLabelText("Feedback fb-1")).toBeInTheDocument();

    // Resolving flips it to "resolved", which no longer matches the active
    // "unreviewed" filter, so the row must disappear rather than linger.
    await user.click(screen.getByLabelText("Resolve feedback"));

    await waitFor(() =>
      expect(screen.queryByLabelText("Feedback fb-1")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("No feedback items")).toBeInTheDocument();
  });

  it("provides promote action", async () => {
    const user = userEvent.setup();
    let promoted = false;

    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(makePage([makeFeedbackItem()])),
      ),
      http.post(`${API}/feedback/:id/promote`, () => {
        promoted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    await screen.findByText("Rating 1");

    await user.click(screen.getByLabelText("Promote to evaluation set"));

    await waitFor(() => expect(promoted).toBe(true));
  });

  it("supports cursor pagination (load more)", async () => {
    const user = userEvent.setup();
    let callCount = 0;

    server.use(
      http.get(`${API}/feedback`, ({ request }) => {
        callCount += 1;
        const url = new URL(request.url);
        const cursor = url.searchParams.get("cursor");

        if (!cursor) {
          return HttpResponse.json(
            makePage([makeFeedbackItem()], "cursor-page-2"),
          );
        }
        return HttpResponse.json(
          makePage([
            makeFeedbackItem({
              feedback: {
                trace_id: "trace-3",
                feedback_id: "fb-3",
                created_at: "2025-05-01T12:00:00Z",
                rating: 2,
                comment: "Page two item",
                expected_answer: null,
                review_status: "unreviewed",
                failure_category: null,
                reviewed_by: null,
                reviewed_at: null,
                promoted_case_id: null,
              },
            }),
          ]),
        );
      }),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    await screen.findByText("Rating 1");

    // Load more button should be visible
    const loadMoreBtn = screen.getByLabelText("Load more feedback");
    expect(loadMoreBtn).toBeInTheDocument();

    await user.click(loadMoreBtn);

    // Second page item should appear
    await screen.findByText("Page two item");
    expect(callCount).toBe(2);
  });

  it("shows SQL field when present", async () => {
    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(
          makePage([makeFeedbackItem({ sql: "SELECT * FROM users WHERE id = 1" })]),
        ),
      ),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    expect(await screen.findByText("SELECT * FROM users WHERE id = 1")).toBeInTheDocument();
  });

  it("shows empty-value placeholders for absent fields (R6.3)", async () => {
    server.use(
      http.get(`${API}/feedback`, () =>
        HttpResponse.json(
          makePage([
            makeFeedbackItem({
              feedback: {
                trace_id: "trace-4",
                feedback_id: "fb-4",
                created_at: "2025-06-01T12:00:00Z",
                rating: 1,
                comment: null,
                expected_answer: null,
                review_status: "unreviewed",
                failure_category: null,
                reviewed_by: null,
                reviewed_at: null,
                promoted_case_id: null,
              },
              expected_answer: null,
              confidence: null,
              route: null,
              retrieved_chunks: [],
              sql: null,
            }),
          ]),
        ),
      ),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    await screen.findByText("Rating 1");

    expect(screen.getByText("No comment")).toBeInTheDocument();
    expect(screen.getByText("Not provided")).toBeInTheDocument();
    expect(screen.getAllByText("N/A").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("None").length).toBeGreaterThanOrEqual(1);
  });

  it("shows error state and retry button on fetch failure", async () => {
    const user = userEvent.setup();
    let callCount = 0;

    server.use(
      http.get(`${API}/feedback`, () => {
        callCount += 1;
        // apiClient retries GET 2 additional times on 5xx, so return 500
        // for the first 3 attempts to exhaust retries, then succeed.
        if (callCount <= 3) {
          return HttpResponse.json({ detail: "Server error" }, { status: 500 });
        }
        return HttpResponse.json(makePage([makeFeedbackItem()]));
      }),
    );

    renderWithProviders(<FeedbackInboxPage />, { route: "/feedback" });

    // Error state should appear after retries are exhausted
    await screen.findByRole("alert");
    expect(screen.getByText(/server error/i)).toBeInTheDocument();

    // Retry
    await user.click(screen.getByText("Retry"));
    await screen.findByText("Rating 1");
  });
});
