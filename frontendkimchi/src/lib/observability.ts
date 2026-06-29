import type { Trace } from "../api/types";
import { CONSOLE_TRAFFIC_ROUTES } from "./constants";

export type TimePreset = "15m" | "1h" | "6h" | "24h" | "custom";

export const TIME_PRESETS: { value: TimePreset; label: string }[] = [
  { value: "15m", label: "15 minutes" },
  { value: "1h", label: "1 hour" },
  { value: "6h", label: "6 hours" },
  { value: "24h", label: "24 hours" },
  { value: "custom", label: "Custom" },
];

const PRESET_MS: Record<Exclude<TimePreset, "custom">, number> = {
  "15m": 15 * 60_000,
  "1h": 60 * 60_000,
  "6h": 6 * 60 * 60_000,
  "24h": 24 * 60 * 60_000,
};

/** Resolve a preset into an ISO start/end window ending now. */
export function presetWindow(preset: Exclude<TimePreset, "custom">): {
  start: string;
  end: string;
} {
  const now = Date.now();
  return {
    start: new Date(now - PRESET_MS[preset]).toISOString(),
    end: new Date(now).toISOString(),
  };
}

export function isConsoleTraffic(route: string): boolean {
  if (CONSOLE_TRAFFIC_ROUTES.includes(route)) return true;
  // /traces/{id} and /logs/{id} variants.
  return CONSOLE_TRAFFIC_ROUTES.some((r) => route.startsWith(`${r}/`));
}

export interface TraceSummary {
  count: number;
  errorRate: number;
  p95Ms: number | null;
  slowestRoute: string | null;
}

/** Compute summary metrics over the loaded window (client side). */
export function computeSummary(traces: Trace[]): TraceSummary {
  const count = traces.length;
  if (count === 0) {
    return { count: 0, errorRate: 0, p95Ms: null, slowestRoute: null };
  }

  const errors = traces.filter((t) => t.root_status === "error").length;
  const errorRate = errors / count;

  const durations = traces.map((t) => t.duration_ms).sort((a, b) => a - b);
  const p95Index = Math.min(durations.length - 1, Math.ceil(0.95 * durations.length) - 1);
  const p95Ms = durations[Math.max(0, p95Index)];

  // Slowest route: highest average duration, requires >= 2 loaded traces.
  const byRoute = new Map<string, number[]>();
  for (const t of traces) {
    const list = byRoute.get(t.route) ?? [];
    list.push(t.duration_ms);
    byRoute.set(t.route, list);
  }
  let slowestRoute: string | null = null;
  let slowestAvg = -1;
  for (const [route, list] of byRoute) {
    if (list.length < 2) continue;
    const avg = list.reduce((a, b) => a + b, 0) / list.length;
    if (avg > slowestAvg) {
      slowestAvg = avg;
      slowestRoute = route;
    }
  }

  return { count, errorRate, p95Ms, slowestRoute };
}

export interface RouteSlice {
  route: string;
  count: number;
  fraction: number;
}

export function routeDistribution(traces: Trace[]): RouteSlice[] {
  const byRoute = new Map<string, number>();
  for (const t of traces) {
    byRoute.set(t.route, (byRoute.get(t.route) ?? 0) + 1);
  }
  const total = traces.length || 1;
  return [...byRoute.entries()]
    .map(([route, count]) => ({ route, count, fraction: count / total }))
    .sort((a, b) => b.count - a.count);
}
