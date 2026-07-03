import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import KnowledgeGapMapPage from "./KnowledgeGapMapPage";
import { renderWithProviders } from "../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../test/server";
import type { KnowledgeGapMap } from "../api/types";

function sampleGapMap(overrides: Partial<KnowledgeGapMap> = {}): KnowledgeGapMap {
  return {
    topics: [
      { topic: "Database indexing", coverage_quality: "poor", contributing_question_count: 12 },
      { topic: "Authentication flows", coverage_quality: "fair", contributing_question_count: 7 },
      { topic: "API rate limiting", coverage_quality: "good", contributing_question_count: 3 },
    ],
    recommended_missing_topics: ["Caching strategies", "Error handling patterns"],
    documents_needing_reingestion: ["doc-123"],
    suggested_benchmark_cases: ["How does caching work?"],
    frequently_requested_topics: ["Database indexing"],
    eligible_outcome_count: 50,
    configured_minimum: 20,
    ...overrides,
  };
}

describe("KnowledgeGapMapPage", () => {
  it("renders the generate button and empty state initially", () => {
    renderWithProviders(<KnowledgeGapMapPage />, { route: "/knowledge-gap-map" });

    expect(
      screen.getByRole("button", { name: /generate knowledge gap map/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/no gap map generated yet/i)).toBeInTheDocument();
  });

  it("displays topics with coverage quality and question counts after generation", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API}/knowledge-gap-map`, () =>
        HttpResponse.json(sampleGapMap()),
      ),
    );

    renderWithProviders(<KnowledgeGapMapPage />, { route: "/knowledge-gap-map" });

    await user.click(screen.getByRole("button", { name: /generate knowledge gap map/i }));

    // Topics should be visible (R11.3) — "Database indexing" appears both as
    // a topic and as a frequently_requested_topics recommendation, so use getAllByText.
    const dbIndexingElements = await screen.findAllByText("Database indexing");
    expect(dbIndexingElements.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Authentication flows")).toBeInTheDocument();
    expect(screen.getByText("API rate limiting")).toBeInTheDocument();

    // Coverage quality badges are rendered
    expect(screen.getByText("Poor")).toBeInTheDocument();
    expect(screen.getByText("Fair")).toBeInTheDocument();
    expect(screen.getByText("Good")).toBeInTheDocument();

    // Contributing question counts are rendered
    expect(screen.getByText("12 questions")).toBeInTheDocument();
    expect(screen.getByText("7 questions")).toBeInTheDocument();
    expect(screen.getByText("3 questions")).toBeInTheDocument();
  });

  it("displays recommendations organized by category", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API}/knowledge-gap-map`, () =>
        HttpResponse.json(sampleGapMap()),
      ),
    );

    renderWithProviders(<KnowledgeGapMapPage />, { route: "/knowledge-gap-map" });
    await user.click(screen.getByRole("button", { name: /generate knowledge gap map/i }));

    // Wait for recommendations section to appear
    await screen.findByText("Caching strategies");

    // Recommendations by category
    expect(screen.getByText("Missing topics")).toBeInTheDocument();
    expect(screen.getByText("Caching strategies")).toBeInTheDocument();
    expect(screen.getByText("Error handling patterns")).toBeInTheDocument();

    expect(screen.getByText("Documents needing re-ingestion")).toBeInTheDocument();
    expect(screen.getByText("doc-123")).toBeInTheDocument();

    expect(screen.getByText("Suggested benchmark cases")).toBeInTheDocument();
    expect(screen.getByText("How does caching work?")).toBeInTheDocument();

    expect(screen.getByText("Frequently requested topics")).toBeInTheDocument();
  });

  it("shows an insufficient-outcomes notice stating the configured minimum (R11.6)", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API}/knowledge-gap-map`, () =>
        HttpResponse.json(
          sampleGapMap({
            eligible_outcome_count: 8,
            configured_minimum: 20,
            topics: [],
          }),
        ),
      ),
    );

    renderWithProviders(<KnowledgeGapMapPage />, { route: "/knowledge-gap-map" });
    await user.click(screen.getByRole("button", { name: /generate knowledge gap map/i }));

    // The notice should mention the configured minimum
    const notice = await screen.findByText(/requires at least/i);
    expect(notice).toBeInTheDocument();
    expect(notice.textContent).toContain("20");
    expect(notice.textContent).toContain("8");
  });

  it("shows an error state when generation fails", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API}/knowledge-gap-map`, () =>
        HttpResponse.json(
          { detail: "knowledge_gap_generation_failed" },
          { status: 500 },
        ),
      ),
    );

    renderWithProviders(<KnowledgeGapMapPage />, { route: "/knowledge-gap-map" });
    await user.click(screen.getByRole("button", { name: /generate knowledge gap map/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/knowledge gap map generation failed/i),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("knowledge_gap_generation_failed")).toBeInTheDocument();

    // Retry button is available
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("uses singular 'question' for a count of 1", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API}/knowledge-gap-map`, () =>
        HttpResponse.json(
          sampleGapMap({
            topics: [
              { topic: "Solo topic", coverage_quality: "poor", contributing_question_count: 1 },
            ],
          }),
        ),
      ),
    );

    renderWithProviders(<KnowledgeGapMapPage />, { route: "/knowledge-gap-map" });
    await user.click(screen.getByRole("button", { name: /generate knowledge gap map/i }));

    expect(await screen.findByText("1 question")).toBeInTheDocument();
  });
});
