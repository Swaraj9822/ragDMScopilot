/**
 * Pure helpers for the "AI Retrieval Investigator". They turn the data we
 * already have — the copilot answer (`UnifiedQueryResponse`) and, when loaded,
 * the correlated observability `Trace` — into a plain-language, factual account
 * of how the answer was produced. Every value is derived from real fields; the
 * module invents nothing, so the findings can never contradict the answer.
 *
 * Kept free of React/DOM so it can be unit-tested directly.
 */

import type { Trace, UnifiedQueryResponse } from "../../api/types";
import { routeLabel } from "../../lib/status";

export type FindingTone = "ok" | "warn" | "info";

/** One line in the investigation checklist. */
export interface Finding {
  tone: FindingTone;
  text: string;
}

/** A simplified processing step derived from a trace span. */
export interface Stage {
  operation: string;
  durationMs: number;
  status: "success" | "error";
}

function plural(n: number, singular: string, pluralForm = `${singular}s`): string {
  return n === 1 ? singular : pluralForm;
}

/** Tone for a confidence score in [0, 1]. */
function confidenceTone(score: number): FindingTone {
  if (score >= 0.8) return "ok";
  if (score >= 0.6) return "info";
  return "warn";
}

/**
 * Derive the ordered checklist of findings for an answer. When a correlated
 * trace is supplied, a couple of trace-only facts (errored steps) are appended.
 */
export function buildFindings(
  response: UnifiedQueryResponse,
  trace?: Trace,
): Finding[] {
  const findings: Finding[] = [];

  // 1. Routing decision.
  const label = routeLabel(response.route);
  findings.push({
    tone: "ok",
    text: response.routing_reasoning
      ? `Routed to ${label} — ${response.routing_reasoning}`
      : `Routed to ${label}.`,
  });

  // 2. Retrieval (only meaningful for document-backed routes).
  if (response.route === "rag" || response.route === "hybrid") {
    const n = response.citations.length;
    findings.push(
      n > 0
        ? { tone: "ok", text: `Retrieved and cited ${n} document ${plural(n, "source")}.` }
        : { tone: "warn", text: "No document sources were cited for this answer." },
    );
  }

  // 3. SQL generation.
  if (response.sql) {
    findings.push({ tone: "ok", text: "Generated a SQL query to read operational data." });
  }

  // 4. Rows returned.
  if (response.rows.length > 0) {
    const n = response.rows.length;
    findings.push({ tone: "ok", text: `The query returned ${n} ${plural(n, "row")}.` });
  } else if (response.sql) {
    findings.push({ tone: "warn", text: "The query returned no rows." });
  }

  // 5. Tables read.
  if (response.data_sources.length > 0) {
    const tables = response.data_sources.map((s) => s.table).join(", ");
    const n = response.data_sources.length;
    findings.push({ tone: "ok", text: `Read from ${n} ${plural(n, "table")}: ${tables}.` });
  }

  // 6. Claim-level support.
  if (response.claim_decomposition_failed) {
    findings.push({
      tone: "info",
      text: "The answer could not be broken into verifiable claims.",
    });
  } else if (response.claims.length > 0) {
    const total = response.claims.length;
    const supported = response.claims.filter(
      (c) => c.evidence_status === "supported",
    ).length;
    findings.push({
      tone: supported === total ? "ok" : "warn",
      text: `${supported} of ${total} ${plural(total, "claim")} fully supported by evidence.`,
    });
  }

  // 7. Confidence.
  if (response.confidence_score != null) {
    const score = response.confidence_score;
    findings.push({
      tone: confidenceTone(score),
      text: `Confidence ${score.toFixed(2)}${
        response.confidence ? ` (${response.confidence})` : ""
      }.`,
    });
  }

  // 8. Evidence status (skip when it merely restates a grounded/ok answer).
  const status = response.evidence_status;
  if (status && status !== "grounded" && status !== "ok") {
    findings.push({
      tone: status.includes("insufficient") || status.includes("unsupported") ? "warn" : "info",
      text: `Evidence status: ${status.replaceAll("_", " ")}.`,
    });
  }

  // 9. Explicit insufficient-evidence reason.
  if (response.insufficient_evidence_reason) {
    findings.push({ tone: "warn", text: response.insufficient_evidence_reason });
  }

  // 10. Trace-only: any step that errored during processing.
  if (trace) {
    const errored = trace.spans.filter((s) => s.status === "error").length;
    if (errored > 0) {
      findings.push({
        tone: "warn",
        text: `${errored} processing ${plural(errored, "step")} reported an error.`,
      });
    }
  }

  return findings;
}

/**
 * Reduce a trace to an ordered list of processing stages (operation, duration,
 * status) for a compact timeline. Ordered by span start time.
 */
export function buildStages(trace: Trace): Stage[] {
  return [...trace.spans]
    .sort((a, b) => a.start_ts.localeCompare(b.start_ts))
    .map((s) => ({
      operation: s.operation,
      durationMs: s.duration_ms,
      status: s.status === "error" ? "error" : "success",
    }));
}
