/**
 * Reshape a copilot answer for display.
 *
 * Database/hybrid answers arrive in a rigid template:
 *
 *   Summary: <prose> Results: <raw JSON array> Conclusion: <prose>
 *
 * That is noisy: the "Summary:" label is redundant (this is an answer, not a
 * summary), and the raw "Results:" JSON dump is unreadable. This helper turns
 * the template into clean markdown — a short lead paragraph, the data as a
 * bullet list built from the structured `rows` (never the fragile inline JSON),
 * then the conclusion.
 *
 * Answers that do not match the template (e.g. free-form RAG prose) are returned
 * unchanged so nothing is lost.
 *
 * Kept free of React/DOM so it can be unit-tested directly.
 */

const MAX_BULLETS = 20;

/** Find the character index just past the first `label:` marker, or -1. */
function labelEnd(lower: string, label: string): number {
  const re = new RegExp(`${label}\\s*:`);
  const idx = lower.search(re);
  if (idx < 0) return -1;
  const colon = lower.indexOf(":", idx);
  return colon < 0 ? -1 : colon + 1;
}

/** Position of the first `label:` marker, or -1. */
function labelStart(lower: string, label: string): number {
  return lower.search(new RegExp(`${label}\\s*:`));
}

function humanizeKey(key: string): string {
  return key.replace(/_/g, " ").trim();
}

function formatValue(value: unknown): string {
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString()
      : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  if (value == null) return "—";
  return String(value);
}

/** Render one row object as a readable markdown bullet. */
function rowToBullet(row: Record<string, unknown>): string {
  const entries = Object.entries(row);
  if (entries.length === 0) return "";

  // Use the first string-valued field as the bullet's headline label.
  let labelIndex = entries.findIndex(([, v]) => typeof v === "string");
  if (labelIndex < 0) labelIndex = 0;

  const label = formatValue(entries[labelIndex][1]);
  const rest = entries
    .filter((_, i) => i !== labelIndex)
    .map(([k, v]) => `${humanizeKey(k)}: ${formatValue(v)}`);

  return rest.length > 0 ? `**${label}** — ${rest.join(", ")}` : label;
}

function buildBullets(rows: Record<string, unknown>[]): string[] {
  const bullets = rows.slice(0, MAX_BULLETS).map(rowToBullet).filter(Boolean);
  if (rows.length > MAX_BULLETS) {
    bullets.push(`…and ${rows.length - MAX_BULLETS} more`);
  }
  return bullets;
}

/**
 * Produce the markdown to display for `answer`, drawing bullet data from the
 * structured `rows` when the answer follows the Summary/Results/Conclusion
 * template.
 */
export function formatAnswer(
  answer: string,
  rows: Record<string, unknown>[] = [],
): string {
  if (!answer) return answer;

  const lower = answer.toLowerCase();
  const iSummary = labelStart(lower, "summary");
  const iResults = labelStart(lower, "results");
  const iConclusion = labelStart(lower, "conclusion");

  // Not the structured template — leave the answer exactly as-is.
  if (iSummary < 0 && iResults < 0 && iConclusion < 0) {
    return answer;
  }

  // Intro: text after "Summary:" (or the start) up to Results/Conclusion.
  const introStart = iSummary >= 0 ? labelEnd(lower, "summary") : 0;
  const stops = [iResults, iConclusion].filter((i) => i > introStart);
  const introEnd = stops.length > 0 ? Math.min(...stops) : answer.length;
  const intro = answer.slice(introStart, introEnd).trim();

  // Conclusion: text after "Conclusion:".
  const conclusion =
    iConclusion >= 0 ? answer.slice(labelEnd(lower, "conclusion")).trim() : "";

  const bullets = buildBullets(rows);

  const parts: string[] = [];
  if (intro) parts.push(intro);
  if (bullets.length > 0) parts.push(bullets.map((b) => `- ${b}`).join("\n"));
  if (conclusion) parts.push(`**Conclusion:** ${conclusion}`);

  // Nothing usable was extracted (e.g. only a "Results:" blob with no rows):
  // fall back to the original answer rather than showing an empty card.
  if (parts.length === 0) return answer;

  return parts.join("\n\n");
}
