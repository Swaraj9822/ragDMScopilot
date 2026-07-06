/**
 * Pure helper that bounds the optional Chart_Spec insight line.
 *
 * The AutoDashboard renders at most one insight line derived from the
 * Chart_Spec, of at most 200 characters (Requirement 10.5). Rather than trust
 * the language model to respect that bound, the frontend re-applies it locally:
 * the raw insight is collapsed to a single line, trimmed, and truncated to at
 * most {@link MAX_INSIGHT_LENGTH} characters. When there is no meaningful
 * insight, `null` is returned so the page can omit the line entirely.
 *
 * This module is intentionally free of React so it can be imported directly by
 * a Vitest test (task 13.4 property-tests it) and by the AutoDashboard
 * component (wired in task 13.5).
 */

/**
 * Maximum length of the rendered insight line, in characters (Requirement
 * 10.5). Mirrors the backend `MAX_INSIGHT_LENGTH` constant in
 * `rag_system/sql_lab/chart_spec.py` so both layers agree on the bound.
 */
export const MAX_INSIGHT_LENGTH = 200;

/**
 * Collapse the optional Chart_Spec insight to at most one line of at most
 * {@link MAX_INSIGHT_LENGTH} characters.
 *
 * The transformation is:
 * 1. Treat a missing/null/undefined insight as absent.
 * 2. Collapse every run of whitespace (including newlines and tabs) to a single
 *    space, so the result is always a single line.
 * 3. Trim leading and trailing whitespace.
 * 4. Truncate to at most {@link MAX_INSIGHT_LENGTH} characters.
 *
 * Returns `null` when there is no insight or the insight is blank once
 * collapsed, so callers can render exactly one line or none at all.
 */
export function boundedInsight(insight?: string | null): string | null {
  if (insight == null) {
    return null;
  }

  const singleLine = insight.replace(/\s+/g, " ").trim();
  if (singleLine.length === 0) {
    return null;
  }

  return singleLine.slice(0, MAX_INSIGHT_LENGTH);
}
