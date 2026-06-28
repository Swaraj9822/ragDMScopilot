export type StatusTone = "good" | "progress" | "warning" | "failure" | "inactive";

const TONE_MAP: Record<string, StatusTone> = {
  // good
  success: "good",
  indexed: "good",
  grounded: "good",
  ok: "good",
  // progress
  queued: "progress",
  parsing: "progress",
  chunking: "progress",
  embedding: "progress",
  // warning
  partially_grounded: "warning",
  insufficient_evidence: "warning",
  warning: "warning",
  // failure
  failed: "failure",
  error: "failure",
  critical: "failure",
  // inactive
  deleted: "inactive",
};

export function statusTone(value: string | null | undefined): StatusTone {
  if (!value) return "inactive";
  return TONE_MAP[value.toLowerCase()] ?? "inactive";
}

export const TONE_COLOR_VAR: Record<StatusTone, string> = {
  good: "--success",
  progress: "--info",
  warning: "--warning",
  failure: "--danger",
  inactive: "--text-muted",
};

export const TONE_SOFT_VAR: Record<StatusTone, string> = {
  good: "--success-soft",
  progress: "--info-soft",
  warning: "--warning-soft",
  failure: "--danger-soft",
  inactive: "--bg-subtle",
};

/** Map an API route value to a human UI label. */
export function routeLabel(route: string): string {
  switch (route) {
    case "rag":
      return "Document";
    case "database":
      return "Database";
    case "hybrid":
      return "Hybrid";
    default:
      return route;
  }
}
