import type { TimePreset } from "../../lib/observability";

export interface TraceFilterState {
  preset: TimePreset;
  customStart: string; // datetime-local value
  customEnd: string;
  route: string;
  status: "all" | "success" | "error";
  minDurationMs: string;
  limit: number;
}

export const DEFAULT_TRACE_FILTERS: TraceFilterState = {
  preset: "1h",
  customStart: "",
  customEnd: "",
  route: "",
  status: "all",
  minDurationMs: "",
  limit: 100,
};

export const LIMIT_OPTIONS = [50, 100, 250, 500, 1000];

export interface FilterValidation {
  valid: boolean;
  errors: Partial<Record<keyof TraceFilterState, string>>;
}

/** Validate filters before issuing a request (end >= start, ranges). */
export function validateTraceFilters(filters: TraceFilterState): FilterValidation {
  const errors: FilterValidation["errors"] = {};

  if (filters.preset === "custom") {
    if (filters.customStart && filters.customEnd) {
      const start = new Date(filters.customStart).getTime();
      const end = new Date(filters.customEnd).getTime();
      if (!Number.isNaN(start) && !Number.isNaN(end) && end < start) {
        errors.customEnd = "End must not be earlier than start.";
      }
    }
  }

  if (filters.minDurationMs !== "") {
    const value = Number(filters.minDurationMs);
    if (Number.isNaN(value) || value < 0 || value > 86_400_000) {
      errors.minDurationMs = "Must be between 0 and 86400000 ms.";
    }
  }

  if (filters.limit < 1 || filters.limit > 1000) {
    errors.limit = "Limit must be between 1 and 1000.";
  }

  return { valid: Object.keys(errors).length === 0, errors };
}

/** Convert a datetime-local string to an ISO-8601 string with timezone. */
export function toIso(local: string): string | null {
  if (!local) return null;
  const date = new Date(local);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}
