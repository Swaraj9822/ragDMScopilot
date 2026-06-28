import type { Span } from "../api/types";

export interface WaterfallRow {
  span: Span;
  depth: number;
  /** Fractional left offset in [0, 1]. */
  offset: number;
  /** Fractional width in [0, 1], minimum enforced by the renderer. */
  width: number;
  startMs: number;
  endMs: number;
}

export interface WaterfallLayout {
  rows: WaterfallRow[];
  /** Earliest span start, ms epoch. */
  originMs: number;
  /** Total span in ms used for scaling (>= 0). */
  totalMs: number;
}

const DEPTH_CAP = 6;

/**
 * Compute the depth of each span by walking parent_span_id, guarding against
 * missing parents (treated as roots) and cycles (capped traversal).
 */
export function computeDepths(spans: Span[]): Map<string, number> {
  const byId = new Map<string, Span>();
  for (const s of spans) byId.set(s.span_id, s);

  const depthCache = new Map<string, number>();

  function depthOf(span: Span): number {
    const cached = depthCache.get(span.span_id);
    if (cached !== undefined) return cached;

    let depth = 0;
    let current: Span | undefined = span;
    const seen = new Set<string>();
    while (current && current.parent_span_id) {
      if (seen.has(current.span_id)) break; // cycle guard
      seen.add(current.span_id);
      const parent = byId.get(current.parent_span_id);
      if (!parent) break; // missing parent -> stop, treat remaining as root chain
      depth += 1;
      current = parent;
      if (depth > spans.length) break; // hard stop against pathological input
    }
    depthCache.set(span.span_id, depth);
    return depth;
  }

  const result = new Map<string, number>();
  for (const s of spans) result.set(s.span_id, depthOf(s));
  return result;
}

/** Visual depth, capped after DEPTH_CAP levels. */
export function cappedDepth(depth: number): number {
  return Math.min(depth, DEPTH_CAP);
}

/**
 * Build waterfall layout rows from spans. The timeline origin is the earliest
 * span start. If the total duration is zero, widths fall back to a minimum so
 * we never divide by zero.
 */
export function computeWaterfall(spans: Span[]): WaterfallLayout {
  if (spans.length === 0) {
    return { rows: [], originMs: 0, totalMs: 0 };
  }

  const depths = computeDepths(spans);

  const starts = spans.map((s) => new Date(s.start_ts).getTime());
  const validStarts = starts.map((t) => (Number.isNaN(t) ? 0 : t));
  const originMs = Math.min(...validStarts);

  let maxEnd = originMs;
  for (let i = 0; i < spans.length; i += 1) {
    const end = validStarts[i] + Math.max(0, spans[i].duration_ms);
    if (end > maxEnd) maxEnd = end;
  }
  const totalMs = Math.max(0, maxEnd - originMs);

  // Preserve incoming order so parallel siblings remain on separate rows.
  const rows: WaterfallRow[] = spans.map((span, i) => {
    const startMs = validStarts[i] - originMs;
    const durationMs = Math.max(0, span.duration_ms);
    const offset = totalMs > 0 ? startMs / totalMs : 0;
    const width = totalMs > 0 ? durationMs / totalMs : 0;
    return {
      span,
      depth: depths.get(span.span_id) ?? 0,
      offset: clamp01(offset),
      width: clamp01(width),
      startMs,
      endMs: startMs + durationMs,
    };
  });

  return { rows, originMs, totalMs };
}

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}
