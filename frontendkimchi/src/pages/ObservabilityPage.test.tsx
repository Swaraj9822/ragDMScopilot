import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import ObservabilityPage from "./ObservabilityPage";
import { renderWithProviders } from "../test/renderWithProviders";
import { API, http, HttpResponse, server } from "../test/server";
import type { Trace } from "../api/types";

const TRACE_ID = "c".repeat(32);

function sampleTrace(): Trace {
  return {
    trace_id: TRACE_ID,
    route: "/ask",
    start_ts: "2026-01-01T00:00:00.000Z",
    duration_ms: 1500,
    root_status: "success",
    spans: [
      {
        span_id: "root",
        parent_span_id: null,
        operation: "handle_ask",
        start_ts: "2026-01-01T00:00:00.000Z",
        duration_ms: 1500,
        status: "success",
        attributes: { route: "rag" },
      },
    ],
  };
}

describe("ObservabilityPage", () => {
  it("opens on the Traces tab when no view is specified", async () => {
    server.use(http.get(`${API}/traces`, () => HttpResponse.json([])));
    renderWithProviders(<ObservabilityPage />, { route: "/observability" });

    const tracesTab = await screen.findByRole("tab", { name: /^traces$/i });
    expect(tracesTab).toHaveAttribute("aria-selected", "true");
    // The middle "Individual Query" tab must not be the landing view.
    expect(
      screen.getByRole("tab", { name: /individual query/i }),
    ).toHaveAttribute("aria-selected", "false");
  });

  it("honors an explicit ?view=queries in the URL", async () => {
    server.use(http.get(`${API}/traces`, () => HttpResponse.json([])));
    renderWithProviders(<ObservabilityPage />, {
      route: "/observability?view=queries",
    });

    expect(
      await screen.findByRole("tab", { name: /individual query/i }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("shows the empty-window state when no traces match", async () => {
    server.use(http.get(`${API}/traces`, () => HttpResponse.json([])));
    renderWithProviders(<ObservabilityPage />, { route: "/observability" });
    expect(await screen.findByText(/no traces match this window/i)).toBeInTheDocument();
  });

  it("fetches and selects a deep-linked trace not present in the list", async () => {
    server.use(
      http.get(`${API}/traces`, () => HttpResponse.json([])),
      http.get(`${API}/traces/${TRACE_ID}`, () => HttpResponse.json(sampleTrace())),
      http.get(`${API}/logs/${TRACE_ID}`, () => HttpResponse.json([])),
    );

    renderWithProviders(<ObservabilityPage />, {
      route: `/observability?trace=${TRACE_ID}`,
    });

    // The deep-linked trace detail renders its span waterfall.
    expect((await screen.findAllByText("handle_ask")).length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(screen.getByText(/no persisted logs for this trace/i)).toBeInTheDocument(),
    );
  });

  it("shows a not-found message for a deep link outside retention", async () => {
    server.use(
      http.get(`${API}/traces`, () => HttpResponse.json([])),
      http.get(`${API}/traces/${TRACE_ID}`, () =>
        HttpResponse.json({ detail: "Trace not found." }, { status: 404 }),
      ),
    );
    renderWithProviders(<ObservabilityPage />, {
      route: `/observability?trace=${TRACE_ID}`,
    });
    expect(
      await screen.findByText(/this trace was not found/i),
    ).toBeInTheDocument();
  });
});
