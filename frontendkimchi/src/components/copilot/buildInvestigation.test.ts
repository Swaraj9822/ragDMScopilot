import { describe, expect, it } from "vitest";
import type { Trace, UnifiedQueryResponse } from "../../api/types";
import { buildFindings, buildStages } from "./buildInvestigation";

function makeResponse(
  overrides: Partial<UnifiedQueryResponse> = {},
): UnifiedQueryResponse {
  return {
    answer: "Answer text.",
    route: "database",
    evidence_status: "grounded",
    trace_id: "a".repeat(32),
    citations: [],
    confidence: null,
    confidence_score: null,
    insufficient_evidence_reason: null,
    sql: null,
    rows: [],
    data_sources: [],
    routing_reasoning: null,
    conversation_id: null,
    rewritten_question: null,
    claims: [],
    claim_decomposition_failed: false,
    ...overrides,
  };
}

function texts(response: UnifiedQueryResponse, trace?: Trace): string[] {
  return buildFindings(response, trace).map((f) => f.text);
}

describe("buildFindings", () => {
  it("always starts with the routing decision and includes the reasoning when present", () => {
    const findings = buildFindings(
      makeResponse({ route: "hybrid", routing_reasoning: "question involved documents + SQL" }),
    );
    expect(findings[0]).toEqual({
      tone: "ok",
      text: "Routed to Hybrid — question involved documents + SQL",
    });
  });

  it("states the route plainly when there is no reasoning", () => {
    expect(texts(makeResponse({ route: "database" }))[0]).toBe("Routed to Database.");
  });

  it("reports cited sources for document-backed routes", () => {
    const withCitations = makeResponse({
      route: "rag",
      citations: [
        { document_id: "d1", chunk_id: "c1", page_start: 1, page_end: 2, title: "Doc" },
        { document_id: "d2", chunk_id: "c2", page_start: null, page_end: null, title: null },
      ],
    });
    expect(texts(withCitations)).toContain("Retrieved and cited 2 document sources.");
  });

  it("warns when a document-backed route cited nothing", () => {
    const findings = buildFindings(makeResponse({ route: "rag", citations: [] }));
    const retrieval = findings.find((f) => f.text.includes("No document sources"));
    expect(retrieval).toEqual({
      tone: "warn",
      text: "No document sources were cited for this answer.",
    });
  });

  it("does not add a retrieval finding for the database route", () => {
    expect(texts(makeResponse({ route: "database" })).join(" ")).not.toMatch(
      /retrieved|cited|document sources/i,
    );
  });

  it("reports SQL generation and the row count", () => {
    const t = texts(
      makeResponse({ sql: "SELECT 1", rows: [{ a: 1 }, { a: 2 }, { a: 3 }] }),
    );
    expect(t).toContain("Generated a SQL query to read operational data.");
    expect(t).toContain("The query returned 3 rows.");
  });

  it("uses singular row wording for a single row", () => {
    expect(texts(makeResponse({ sql: "SELECT 1", rows: [{ a: 1 }] }))).toContain(
      "The query returned 1 row.",
    );
  });

  it("warns when SQL ran but returned no rows", () => {
    const findings = buildFindings(makeResponse({ sql: "SELECT 1", rows: [] }));
    expect(findings).toContainEqual({
      tone: "warn",
      text: "The query returned no rows.",
    });
  });

  it("lists the tables that were read", () => {
    const t = texts(
      makeResponse({
        data_sources: [
          { table: "orders", columns: ["id"] },
          { table: "customers", columns: ["email"] },
        ],
      }),
    );
    expect(t).toContain("Read from 2 tables: orders, customers.");
  });

  it("summarises claim support and warns when not all claims are supported", () => {
    const findings = buildFindings(
      makeResponse({
        claims: [
          { claim_id: "1", text: "a", answer_span: { start: 0, end: 1 }, evidence_items: [], evidence_status: "supported" },
          { claim_id: "2", text: "b", answer_span: { start: 1, end: 2 }, evidence_items: [], evidence_status: "unsupported" },
        ],
      }),
    );
    expect(findings).toContainEqual({
      tone: "warn",
      text: "1 of 2 claims fully supported by evidence.",
    });
  });

  it("notes when claim decomposition failed", () => {
    expect(texts(makeResponse({ claim_decomposition_failed: true }))).toContain(
      "The answer could not be broken into verifiable claims.",
    );
  });

  it("tones the confidence finding by score", () => {
    const high = buildFindings(makeResponse({ confidence_score: 0.91 })).find((f) =>
      f.text.startsWith("Confidence"),
    );
    const low = buildFindings(makeResponse({ confidence_score: 0.4 })).find((f) =>
      f.text.startsWith("Confidence"),
    );
    expect(high).toEqual({ tone: "ok", text: "Confidence 0.91." });
    expect(low).toEqual({ tone: "warn", text: "Confidence 0.40." });
  });

  it("includes the confidence label when provided", () => {
    expect(
      texts(makeResponse({ confidence_score: 0.74, confidence: "medium" })),
    ).toContain("Confidence 0.74 (medium).");
  });

  it("surfaces a non-grounded evidence status but skips a grounded one", () => {
    expect(texts(makeResponse({ evidence_status: "insufficient_evidence" }))).toContain(
      "Evidence status: insufficient evidence.",
    );
    expect(texts(makeResponse({ evidence_status: "grounded" })).join(" ")).not.toMatch(
      /evidence status/i,
    );
  });

  it("includes an explicit insufficient-evidence reason", () => {
    expect(
      texts(makeResponse({ insufficient_evidence_reason: "Numbers disagreed." })),
    ).toContain("Numbers disagreed.");
  });

  it("appends an errored-step finding derived from the trace", () => {
    const trace: Trace = {
      trace_id: "a".repeat(32),
      route: "database",
      start_ts: "2026-01-01T00:00:00.000Z",
      duration_ms: 50,
      root_status: "error",
      spans: [
        { span_id: "s1", parent_span_id: null, operation: "route", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 10, status: "success", attributes: {} },
        { span_id: "s2", parent_span_id: "s1", operation: "sql", start_ts: "2026-01-01T00:00:00.010Z", duration_ms: 40, status: "error", attributes: {} },
      ],
    };
    expect(texts(makeResponse(), trace)).toContain(
      "1 processing step reported an error.",
    );
  });
});

describe("buildStages", () => {
  it("orders spans by start time and maps their status", () => {
    const trace: Trace = {
      trace_id: "a".repeat(32),
      route: "database",
      start_ts: "2026-01-01T00:00:00.000Z",
      duration_ms: 50,
      root_status: "success",
      spans: [
        { span_id: "s2", parent_span_id: "s1", operation: "sql.execute", start_ts: "2026-01-01T00:00:00.010Z", duration_ms: 40, status: "error", attributes: {} },
        { span_id: "s1", parent_span_id: null, operation: "route", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 10, status: "success", attributes: {} },
      ],
    };
    expect(buildStages(trace)).toEqual([
      { operation: "route", durationMs: 10, status: "success" },
      { operation: "sql.execute", durationMs: 40, status: "error" },
    ]);
  });
});
