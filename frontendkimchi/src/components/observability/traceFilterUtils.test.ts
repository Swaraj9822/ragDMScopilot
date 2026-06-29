import { describe, expect, it } from "vitest";
import {
  DEFAULT_TRACE_FILTERS,
  toIso,
  validateTraceFilters,
  type TraceFilterState,
} from "./traceFilterUtils";

describe("validateTraceFilters", () => {
  it("accepts default filters", () => {
    expect(validateTraceFilters(DEFAULT_TRACE_FILTERS).valid).toBe(true);
  });

  it("rejects an inverted custom range", () => {
    const filters: TraceFilterState = {
      ...DEFAULT_TRACE_FILTERS,
      preset: "custom",
      customStart: "2026-01-02T00:00",
      customEnd: "2026-01-01T00:00",
    };
    const result = validateTraceFilters(filters);
    expect(result.valid).toBe(false);
    expect(result.errors.customEnd).toBeDefined();
  });

  it("rejects an out-of-range minimum duration", () => {
    const result = validateTraceFilters({
      ...DEFAULT_TRACE_FILTERS,
      minDurationMs: "99999999",
    });
    expect(result.valid).toBe(false);
    expect(result.errors.minDurationMs).toBeDefined();
  });

  it("rejects a limit outside 1..1000", () => {
    expect(validateTraceFilters({ ...DEFAULT_TRACE_FILTERS, limit: 0 }).valid).toBe(false);
    expect(validateTraceFilters({ ...DEFAULT_TRACE_FILTERS, limit: 5000 }).valid).toBe(false);
  });
});

describe("toIso", () => {
  it("returns null for empty input", () => {
    expect(toIso("")).toBeNull();
  });
  it("converts a datetime-local string to ISO with timezone", () => {
    const iso = toIso("2026-01-01T12:00");
    expect(iso).toMatch(/T.*Z$/);
  });
});
