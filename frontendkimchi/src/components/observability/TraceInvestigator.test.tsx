import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TraceInvestigator } from "./TraceInvestigator";
import { renderWithProviders } from "../../test/renderWithProviders";
import { server, http, HttpResponse, API } from "../../test/server";
import type { TraceDiagnosis } from "../../api/types";

const DIAGNOSIS_WITH_CAUSE: TraceDiagnosis = {
  trace_id: "trace-abc-123",
  cause_description:
    "Low retrieval scores indicate that no relevant documents matched the query. The top retrieval score was 0.12, well below the threshold of 0.3.",
  analyzed_elements: ["retrieval_scores", "generation_outcome"],
  recommendations: [
    {
      target: "corpus",
      description: "Add documentation covering the topic of 'deployment pipelines' to improve retrieval relevance.",
    },
    {
      target: "ai_configuration",
      description: "Consider lowering the retrieval_score_threshold from 0.3 to 0.2 to allow borderline results through.",
    },
  ],
};

const DIAGNOSIS_NO_CAUSE: TraceDiagnosis = {
  trace_id: "trace-abc-123",
  cause_description: "No cause determined",
  analyzed_elements: ["route", "retrieval_scores", "rerank_order", "generation_outcome"],
  recommendations: [],
};

describe("TraceInvestigator", () => {
  it("renders a Diagnose button", () => {
    renderWithProviders(<TraceInvestigator traceId="trace-abc-123" />);
    expect(screen.getByRole("button", { name: /diagnose/i })).toBeInTheDocument();
  });

  it("shows cause and recommendations after successful diagnosis (R10.6)", async () => {
    server.use(
      http.post(`${API}/traces/:id/diagnose`, () =>
        HttpResponse.json(DIAGNOSIS_WITH_CAUSE),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<TraceInvestigator traceId="trace-abc-123" />);

    await user.click(screen.getByRole("button", { name: /diagnose/i }));

    // Wait for the result to render
    await waitFor(() => {
      expect(screen.getByLabelText("Diagnosis result")).toBeInTheDocument();
    });

    // Cause description is visible
    expect(
      screen.getByText(/Low retrieval scores indicate/),
    ).toBeInTheDocument();

    // Analyzed elements are shown
    expect(screen.getByText("Retrieval Scores")).toBeInTheDocument();
    expect(screen.getByText("Generation Outcome")).toBeInTheDocument();

    // Recommendations are rendered as read-only suggestions
    expect(screen.getByText(/suggestions only/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Add documentation covering the topic/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Consider lowering the retrieval_score_threshold/),
    ).toBeInTheDocument();

    // Target badges are shown
    expect(screen.getByText("Corpus")).toBeInTheDocument();
    expect(screen.getByText("Config")).toBeInTheDocument();
  });

  it("shows 'no cause determined' state when diagnosis has no recommendations (R10.6)", async () => {
    server.use(
      http.post(`${API}/traces/:id/diagnose`, () =>
        HttpResponse.json(DIAGNOSIS_NO_CAUSE),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<TraceInvestigator traceId="trace-abc-123" />);

    await user.click(screen.getByRole("button", { name: /diagnose/i }));

    await waitFor(() => {
      expect(screen.getByLabelText("Diagnosis result")).toBeInTheDocument();
    });

    // No-cause message is shown
    expect(
      screen.getByText(/No cause determined/),
    ).toBeInTheDocument();

    // No recommendations section
    expect(screen.queryByText(/suggestions only/i)).not.toBeInTheDocument();
  });

  it("shows error message on API failure", async () => {
    server.use(
      http.post(`${API}/traces/:id/diagnose`, () =>
        HttpResponse.json({ detail: "Trace not found" }, { status: 404 }),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<TraceInvestigator traceId="trace-missing" />);

    await user.click(screen.getByRole("button", { name: /diagnose/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });

    expect(screen.getByText(/Trace not found/)).toBeInTheDocument();
  });

  it("disables the button while diagnosing", async () => {
    server.use(
      http.post(`${API}/traces/:id/diagnose`, async () => {
        // Simulate a brief delay
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json(DIAGNOSIS_WITH_CAUSE);
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<TraceInvestigator traceId="trace-abc-123" />);

    const btn = screen.getByRole("button", { name: /diagnose/i });
    await user.click(btn);

    // Button should show loading text and be disabled
    expect(screen.getByRole("button", { name: /diagnose/i })).toBeDisabled();

    // Wait for completion
    await waitFor(() => {
      expect(screen.getByLabelText("Diagnosis result")).toBeInTheDocument();
    });

    // Button is re-enabled after completion
    expect(screen.getByRole("button", { name: /diagnose/i })).toBeEnabled();
  });

  it("presents recommendations as read-only, never as mutation actions (R10.6)", async () => {
    server.use(
      http.post(`${API}/traces/:id/diagnose`, () =>
        HttpResponse.json(DIAGNOSIS_WITH_CAUSE),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<TraceInvestigator traceId="trace-abc-123" />);

    await user.click(screen.getByRole("button", { name: /diagnose/i }));

    await waitFor(() => {
      expect(screen.getByLabelText("Diagnosis result")).toBeInTheDocument();
    });

    // No "Apply", "Execute", "Save", or "Confirm" buttons exist in the results
    const resultRegion = screen.getByLabelText("Diagnosis result");
    const buttons = resultRegion.querySelectorAll("button");
    expect(buttons.length).toBe(0);

    // The read-only notice is displayed
    expect(
      screen.getByText(/suggestions only.*no changes are applied automatically/i),
    ).toBeInTheDocument();
  });
});
