import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AnswerInvestigation } from "./AnswerInvestigation";
import { AnswerCard } from "./AnswerCard";
import { renderWithProviders } from "../../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../../test/server";
import type { Trace, TraceDiagnosis, UnifiedQueryResponse } from "../../api/types";

const TRACE_ID = "a".repeat(32);

function makeResponse(
  overrides: Partial<UnifiedQueryResponse> = {},
): UnifiedQueryResponse {
  return {
    answer: "Culture Ghee 500g generated the most revenue.",
    route: "database",
    evidence_status: "grounded",
    trace_id: TRACE_ID,
    citations: [],
    confidence: null,
    confidence_score: 0.86,
    insufficient_evidence_reason: null,
    sql: "SELECT item_name FROM sales",
    rows: [{ item_name: "Culture Ghee 500g" }],
    data_sources: [{ table: "sales_invoice", columns: ["item_name", "total"] }],
    routing_reasoning: "the question asked for operational figures",
    conversation_id: null,
    rewritten_question: null,
    claims: [],
    claim_decomposition_failed: false,
    ...overrides,
  };
}

function makeTrace(): Trace {
  return {
    trace_id: TRACE_ID,
    route: "database",
    start_ts: "2026-01-01T00:00:00.000Z",
    duration_ms: 50,
    root_status: "success",
    spans: [
      { span_id: "s1", parent_span_id: null, operation: "route.classify", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 10, status: "success", attributes: {} },
      { span_id: "s2", parent_span_id: "s1", operation: "sql.execute", start_ts: "2026-01-01T00:00:00.010Z", duration_ms: 40, status: "success", attributes: {} },
    ],
  };
}

function makeDiagnosis(overrides: Partial<TraceDiagnosis> = {}): TraceDiagnosis {
  return {
    trace_id: TRACE_ID,
    cause_description: "The answer relied on the latest sales rows and is well supported.",
    analyzed_elements: ["route", "generation_outcome"],
    recommendations: [
      { target: "corpus", description: "Upload newer Pricing Policy.pdf" },
    ],
    ...overrides,
  };
}

/** Register the trace + diagnose endpoints the investigator calls when opened. */
function mockInvestigation(opts: {
  trace?: Trace;
  diagnosis?: TraceDiagnosis;
  traceStatus?: number;
  diagnosisStatus?: number;
} = {}) {
  server.use(
    http.get(`${API}/traces/:id`, () => {
      if (opts.traceStatus && opts.traceStatus !== 200) {
        return HttpResponse.json({ detail: "Trace not found." }, { status: opts.traceStatus });
      }
      return HttpResponse.json(opts.trace ?? makeTrace());
    }),
    http.post(`${API}/traces/:id/diagnose`, () => {
      if (opts.diagnosisStatus && opts.diagnosisStatus !== 200) {
        return HttpResponse.json(
          { detail: "The investigation could not be generated." },
          { status: opts.diagnosisStatus },
        );
      }
      return HttpResponse.json(opts.diagnosis ?? makeDiagnosis());
    }),
  );
}

describe("AnswerInvestigation", () => {
  it("hides the investigation until the trigger is clicked", () => {
    mockInvestigation();
    renderWithProviders(<AnswerInvestigation response={makeResponse()} />);

    expect(
      screen.getByRole("button", { name: /why did the ai answer this/i }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/ai investigation/i)).not.toBeInTheDocument();
  });

  it("shows deterministic findings derived from the answer when opened", async () => {
    mockInvestigation();
    const user = userEvent.setup();
    renderWithProviders(<AnswerInvestigation response={makeResponse()} />);

    await user.click(screen.getByRole("button", { name: /why did the ai answer this/i }));

    expect(screen.getByText(/ai investigation/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Routed to Database — the question asked for operational figures/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/generated a sql query/i)).toBeInTheDocument();
    expect(screen.getByText(/the query returned 1 row\./i)).toBeInTheDocument();
    expect(screen.getByText(/read from 1 table: sales_invoice\./i)).toBeInTheDocument();
    expect(screen.getByText(/confidence 0\.86\./i)).toBeInTheDocument();
  });

  it("fetches and renders the AI diagnosis and its suggestions", async () => {
    mockInvestigation();
    const user = userEvent.setup();
    renderWithProviders(<AnswerInvestigation response={makeResponse()} />);

    await user.click(screen.getByRole("button", { name: /why did the ai answer this/i }));

    expect(
      await screen.findByText(/relied on the latest sales rows/i),
    ).toBeInTheDocument();
    const suggestion = screen.getByText(/upload newer pricing policy\.pdf/i);
    expect(suggestion).toBeInTheDocument();
    expect(screen.getByText(/^Corpus$/)).toBeInTheDocument();
  });

  it("renders the processing timeline from the correlated trace", async () => {
    mockInvestigation();
    const user = userEvent.setup();
    renderWithProviders(<AnswerInvestigation response={makeResponse()} />);

    await user.click(screen.getByRole("button", { name: /why did the ai answer this/i }));

    // Wait for the trace to load, then expand the timeline disclosure.
    const timelineToggle = await screen.findByRole("button", {
      name: /processing timeline/i,
    });
    await user.click(timelineToggle);

    expect(await screen.findByText("route.classify")).toBeInTheDocument();
    expect(screen.getByText("sql.execute")).toBeInTheDocument();
  });

  it("surfaces a diagnosis error without hiding the deterministic findings", async () => {
    mockInvestigation({ diagnosisStatus: 500 });
    const user = userEvent.setup();
    renderWithProviders(<AnswerInvestigation response={makeResponse()} />);

    await user.click(screen.getByRole("button", { name: /why did the ai answer this/i }));

    expect(
      await screen.findByText(/the investigation could not be generated/i),
    ).toBeInTheDocument();
    // Findings still render even though the AI narrative failed.
    expect(screen.getByText(/routed to database/i)).toBeInTheDocument();
  });

  it("reveals the generated SQL and result rows inside the panel", async () => {
    mockInvestigation();
    const user = userEvent.setup();
    renderWithProviders(<AnswerInvestigation response={makeResponse()} />);

    await user.click(screen.getByRole("button", { name: /why did the ai answer this/i }));
    await user.click(screen.getByRole("button", { name: /generated sql/i }));

    expect(screen.getByText(/select item_name from sales/i)).toBeInTheDocument();
  });
});

describe("AnswerCard investigator integration", () => {
  it("replaces the old evidence tabs with the single investigator control", () => {
    renderWithProviders(
      <AnswerCard response={makeResponse()} elapsedMs={1200} />,
    );

    // The single investigator button is present...
    expect(
      screen.getByRole("button", { name: /why did the ai answer this/i }),
    ).toBeInTheDocument();

    // ...and the old top-level section toggles are gone (their content is now
    // consolidated inside the closed investigation panel).
    expect(
      screen.queryByRole("button", { name: /^generated sql$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /why this route/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /result rows/i }),
    ).not.toBeInTheDocument();
  });

  it("omits the investigator when there is no trace id", () => {
    renderWithProviders(
      <AnswerCard response={makeResponse({ trace_id: "" })} elapsedMs={500} />,
    );
    expect(
      screen.queryByRole("button", { name: /why did the ai answer this/i }),
    ).not.toBeInTheDocument();
  });
});
