import { describe, expect, it } from "vitest";
import { computeSummary, isConsoleTraffic, routeDistribution } from "./observability";
import type { Trace } from "../api/types";

function trace(partial: Partial<Trace> & { trace_id: string }): Trace {
  return {
    route: "/ask",
    start_ts: "2026-01-01T00:00:00.000Z",
    duration_ms: 100,
    root_status: "success",
    spans: [],
    ...partial,
  };
}

describe("isConsoleTraffic", () => {
  it("flags console-generated routes", () => {
    expect(isConsoleTraffic("/health")).toBe(true);
    expect(isConsoleTraffic("/traces")).toBe(true);
    expect(isConsoleTraffic("/logs/abc")).toBe(true);
    expect(isConsoleTraffic("/metrics")).toBe(true);
  });
  it("keeps application routes", () => {
    expect(isConsoleTraffic("/ask")).toBe(false);
    expect(isConsoleTraffic("ingestion")).toBe(false);
  });
});

describe("computeSummary", () => {
  it("returns zeros for an empty sample", () => {
    expect(computeSummary([])).toEqual({
      count: 0,
      errorRate: 0,
      p95Ms: null,
      slowestRoute: null,
    });
  });

  it("computes count, error rate and slowest route", () => {
    const traces = [
      trace({ trace_id: "1", route: "/ask", duration_ms: 100, root_status: "success" }),
      trace({ trace_id: "2", route: "/ask", duration_ms: 200, root_status: "error" }),
      trace({ trace_id: "3", route: "/query", duration_ms: 1000, root_status: "success" }),
      trace({ trace_id: "4", route: "/query", duration_ms: 1200, root_status: "success" }),
    ];
    const summary = computeSummary(traces);
    expect(summary.count).toBe(4);
    expect(summary.errorRate).toBeCloseTo(0.25, 3);
    // /query has the higher average and >= 2 samples.
    expect(summary.slowestRoute).toBe("/query");
  });

  it("ignores routes with fewer than two traces for slowest route", () => {
    const traces = [
      trace({ trace_id: "1", route: "/slow", duration_ms: 9999 }),
      trace({ trace_id: "2", route: "/ask", duration_ms: 100 }),
      trace({ trace_id: "3", route: "/ask", duration_ms: 120 }),
    ];
    expect(computeSummary(traces).slowestRoute).toBe("/ask");
  });
});

describe("routeDistribution", () => {
  it("sums fractions to 1", () => {
    const traces = [
      trace({ trace_id: "1", route: "/ask" }),
      trace({ trace_id: "2", route: "/ask" }),
      trace({ trace_id: "3", route: "/query" }),
    ];
    const dist = routeDistribution(traces);
    const total = dist.reduce((acc, d) => acc + d.fraction, 0);
    expect(total).toBeCloseTo(1, 5);
    expect(dist[0].route).toBe("/ask");
  });
});
