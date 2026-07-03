import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReplayLabPage from "./ReplayLabPage";
import { renderWithProviders } from "../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../test/server";
import type { ReplayRun, ReplayRunResult } from "../api/types";

function makeResult(overrides: Partial<ReplayRunResult> = {}): ReplayRunResult {
  return {
    answer: "The answer is 42.",
    evidence: [],
    route: "rag",
    retrieval_scores: [0.85, 0.72],
    latency_ms: 1200,
    prompt_tokens: 500,
    completion_tokens: 150,
    cost: 0.0042,
    ...overrides,
  };
}

function makeRun(overrides: Partial<ReplayRun> = {}): ReplayRun {
  return {
    replay_run_id: "run-1",
    state: "completed",
    request: {
      question: "What is the meaning of life?",
      ai_configuration_version_id: "config-v1",
      retrieval_params: { max_passages: 10, min_score: 0.3 },
      corpus_snapshot_id: "snap-1",
    },
    result: makeResult(),
    failure_reason: null,
    cancel_requested: false,
    ...overrides,
  };
}

describe("ReplayLabPage", () => {
  it("renders the page header and initiation form", async () => {
    server.use(
      http.get(`${API}/corpus-snapshots`, () => HttpResponse.json([])),
    );
    renderWithProviders(<ReplayLabPage />);

    expect(screen.getByText("Replay & Compare Lab")).toBeInTheDocument();
    expect(screen.getByLabelText(/^question$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/ai config version id/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start replay/i })).toBeInTheDocument();
  });

  it("loads and displays corpus snapshots in the select dropdown", async () => {
    server.use(
      http.get(`${API}/corpus-snapshots`, () =>
        HttpResponse.json([
          {
            corpus_snapshot_id: "snap-abc123",
            created_at: "2024-06-01T10:00:00Z",
            manifest_size: 15,
          },
        ]),
      ),
    );

    renderWithProviders(<ReplayLabPage />);

    await waitFor(() =>
      expect(screen.getByText(/snap-abc/i)).toBeInTheDocument(),
    );
  });

  it("initiates a replay run and shows it in the runs list", async () => {
    const queuedRun = makeRun({
      replay_run_id: "run-new",
      state: "queued",
      result: null,
      request: {
        question: "Test question",
        ai_configuration_version_id: "config-v1",
        retrieval_params: { max_passages: 10, min_score: 0.3 },
        corpus_snapshot_id: "snap-1",
      },
    });

    server.use(
      http.get(`${API}/corpus-snapshots`, () =>
        HttpResponse.json([
          { corpus_snapshot_id: "snap-1", created_at: "2024-01-01T00:00:00Z", manifest_size: 5 },
        ]),
      ),
      http.post(`${API}/replays`, () => HttpResponse.json(queuedRun)),
    );

    const user = userEvent.setup();
    renderWithProviders(<ReplayLabPage />);

    // Wait for snapshots to load
    await waitFor(() =>
      expect(screen.queryByPlaceholderText(/loading snapshots/i)).not.toBeInTheDocument(),
    );

    await user.type(screen.getByLabelText(/^question$/i), "Test question");
    await user.type(
      screen.getByLabelText(/ai config version id/i),
      "config-v1",
    );
    await user.click(screen.getByRole("button", { name: /start replay/i }));

    expect(await screen.findByText("Test question")).toBeInTheDocument();
    expect(screen.getByText("queued")).toBeInTheDocument();
  });

  it("shows an error message when run creation fails", async () => {
    server.use(
      http.get(`${API}/corpus-snapshots`, () => HttpResponse.json([])),
      http.post(`${API}/replays`, () =>
        HttpResponse.json(
          { detail: "approved_configuration_required" },
          { status: 400 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<ReplayLabPage />);

    await user.type(screen.getByLabelText(/^question$/i), "Bad run");
    await user.type(
      screen.getByLabelText(/ai config version id/i),
      "bad-config",
    );
    // Fill corpus snapshot manually since no dropdown
    await user.type(screen.getByLabelText(/corpus snapshot/i), "snap-x");
    await user.click(screen.getByRole("button", { name: /start replay/i }));

    expect(
      await screen.findByText(/approved_configuration_required/i),
    ).toBeInTheDocument();
  });

  it("displays the comparison view when two completed runs are selected", async () => {
    const runA = makeRun({
      replay_run_id: "run-a",
      request: {
        ...makeRun().request,
        question: "First question",
      },
      result: makeResult({ latency_ms: 1000, cost: 0.003 }),
    });
    const runB = makeRun({
      replay_run_id: "run-b",
      request: {
        ...makeRun().request,
        question: "Second question",
      },
      result: makeResult({ latency_ms: 2000, cost: 0.005 }),
    });

    // We need to initiate two runs first
    let callCount = 0;
    server.use(
      http.get(`${API}/corpus-snapshots`, () =>
        HttpResponse.json([
          { corpus_snapshot_id: "snap-1", created_at: "2024-01-01T00:00:00Z", manifest_size: 5 },
        ]),
      ),
      http.post(`${API}/replays`, () => {
        callCount++;
        return HttpResponse.json(callCount === 1 ? runA : runB);
      }),
      // The runs are immediately completed for this test
      http.get(`${API}/replays/run-a`, () => HttpResponse.json(runA)),
      http.get(`${API}/replays/run-b`, () => HttpResponse.json(runB)),
    );

    const user = userEvent.setup();
    renderWithProviders(<ReplayLabPage />);

    // Wait for snapshots
    await waitFor(() =>
      expect(screen.queryByPlaceholderText(/loading snapshots/i)).not.toBeInTheDocument(),
    );

    const questionInput = screen.getByLabelText(/^question$/i);
    const configInput = screen.getByLabelText(/ai config version id/i);

    // Create first run
    await user.type(questionInput, "First question");
    await user.type(configInput, "cfg1");
    await user.click(screen.getByRole("button", { name: /start replay/i }));
    await screen.findByText("First question");

    // Create second run
    await user.type(questionInput, "Second question");
    await user.clear(configInput);
    await user.type(configInput, "cfg2");
    await user.click(screen.getByRole("button", { name: /start replay/i }));
    await screen.findByText("Second question");

    // Select both for comparison
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);

    // The comparison view should appear
    expect(await screen.findByText(/side-by-side comparison/i)).toBeInTheDocument();
    // Cost values appear in both the metric rows and the per-column detail
    expect(screen.getAllByText(/\$0\.0030/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\$0\.0050/).length).toBeGreaterThan(0);
  });

  it("shows empty state when no runs exist", () => {
    server.use(
      http.get(`${API}/corpus-snapshots`, () => HttpResponse.json([])),
    );
    renderWithProviders(<ReplayLabPage />);
    expect(screen.getByText(/no replay runs yet/i)).toBeInTheDocument();
  });
});
