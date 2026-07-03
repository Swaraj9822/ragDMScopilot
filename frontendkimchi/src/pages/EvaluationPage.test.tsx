import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import EvaluationPage from "./EvaluationPage";
import { renderWithProviders } from "../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../test/server";
import type { EvaluationRunDetail, EvaluationRunSummary } from "../api/evaluation";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const RUN_DETAIL: EvaluationRunDetail = {
  run_id: "run-1",
  created_at: "2025-06-01T12:00:00Z",
  ci_passed: true,
  results: [],
};

const RUN_SUMMARY: EvaluationRunSummary = {
  run_id: "run-1",
  created_at: "2025-06-01T12:00:00Z",
  ci_passed: true,
  result_count: 0,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("EvaluationPage", () => {
  it("shows a run button in the empty state", async () => {
    server.use(http.get(`${API}/evaluation/runs`, () => HttpResponse.json([])));

    renderWithProviders(<EvaluationPage />, { route: "/evaluation" });

    expect(await screen.findByText(/no evaluation runs yet/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /run evaluation/i }),
    ).toBeInTheDocument();
  });

  it("triggers a run via POST /evaluation/runs and surfaces the new run", async () => {
    const user = userEvent.setup();
    let posted = false;
    let listCalls = 0;

    server.use(
      http.get(`${API}/evaluation/runs`, () => {
        listCalls += 1;
        // Empty until a run has been triggered, then it appears in the list.
        return HttpResponse.json(posted ? [RUN_SUMMARY] : []);
      }),
      http.post(`${API}/evaluation/runs`, () => {
        posted = true;
        return HttpResponse.json(RUN_DETAIL);
      }),
      http.get(`${API}/evaluation/runs/run-1`, () => HttpResponse.json(RUN_DETAIL)),
    );

    renderWithProviders(<EvaluationPage />, { route: "/evaluation" });

    await user.click(await screen.findByRole("button", { name: /run evaluation/i }));

    // After the run, the detail (CI banner) is shown for the fresh run.
    await waitFor(() =>
      expect(screen.getByText(/ci status: passed/i)).toBeInTheDocument(),
    );
    expect(posted).toBe(true);
    // The list was refetched after the run completed.
    expect(listCalls).toBeGreaterThanOrEqual(2);
  });

  it("surfaces an error when the run request fails", async () => {
    const user = userEvent.setup();

    server.use(
      http.get(`${API}/evaluation/runs`, () => HttpResponse.json([])),
      http.post(`${API}/evaluation/runs`, () =>
        HttpResponse.json({ detail: "evaluation_set_empty" }, { status: 400 }),
      ),
    );

    renderWithProviders(<EvaluationPage />, { route: "/evaluation" });

    await user.click(await screen.findByRole("button", { name: /run evaluation/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/evaluation_set_empty/i);
  });
});
