import { describe, expect, it } from "vitest";
import { computeDepths, computeWaterfall } from "./waterfall";
import type { Span } from "../api/types";

function span(partial: Partial<Span> & { span_id: string }): Span {
  return {
    parent_span_id: null,
    operation: "op",
    start_ts: "2026-01-01T00:00:00.000Z",
    duration_ms: 100,
    status: "success",
    attributes: {},
    ...partial,
  };
}

describe("computeDepths", () => {
  it("assigns depth by walking parents", () => {
    const spans = [
      span({ span_id: "a", parent_span_id: null }),
      span({ span_id: "b", parent_span_id: "a" }),
      span({ span_id: "c", parent_span_id: "b" }),
    ];
    const depths = computeDepths(spans);
    expect(depths.get("a")).toBe(0);
    expect(depths.get("b")).toBe(1);
    expect(depths.get("c")).toBe(2);
  });

  it("treats a missing parent as a root (depth resilient)", () => {
    const spans = [span({ span_id: "child", parent_span_id: "ghost" })];
    const depths = computeDepths(spans);
    expect(depths.get("child")).toBe(0);
  });

  it("does not loop forever on a cycle", () => {
    const spans = [
      span({ span_id: "x", parent_span_id: "y" }),
      span({ span_id: "y", parent_span_id: "x" }),
    ];
    const depths = computeDepths(spans);
    expect(depths.get("x")).toBeTypeOf("number");
    expect(depths.get("y")).toBeTypeOf("number");
  });
});

describe("computeWaterfall", () => {
  it("scales offsets and widths against the trace duration", () => {
    const spans = [
      span({ span_id: "root", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 1000 }),
      span({ span_id: "mid", start_ts: "2026-01-01T00:00:00.500Z", duration_ms: 250 }),
    ];
    const layout = computeWaterfall(spans);
    expect(layout.totalMs).toBe(1000);
    const mid = layout.rows.find((r) => r.span.span_id === "mid")!;
    expect(mid.offset).toBeCloseTo(0.5, 3);
    expect(mid.width).toBeCloseTo(0.25, 3);
  });

  it("handles a zero-duration trace without dividing by zero", () => {
    const spans = [
      span({ span_id: "a", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 0 }),
      span({ span_id: "b", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 0 }),
    ];
    const layout = computeWaterfall(spans);
    expect(layout.totalMs).toBe(0);
    for (const row of layout.rows) {
      expect(Number.isFinite(row.offset)).toBe(true);
      expect(Number.isFinite(row.width)).toBe(true);
    }
  });

  it("keeps parallel siblings on separate rows", () => {
    const spans = [
      span({ span_id: "p", parent_span_id: null, duration_ms: 100 }),
      span({ span_id: "s1", parent_span_id: "p", duration_ms: 50 }),
      span({ span_id: "s2", parent_span_id: "p", duration_ms: 50 }),
    ];
    const layout = computeWaterfall(spans);
    expect(layout.rows).toHaveLength(3);
  });

  it("returns an empty layout for no spans", () => {
    const layout = computeWaterfall([]);
    expect(layout.rows).toHaveLength(0);
    expect(layout.totalMs).toBe(0);
  });
});
