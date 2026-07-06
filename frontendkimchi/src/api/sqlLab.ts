// Typed API layer for SQL Lab (operator-only read-only data explorer).
// All network access funnels through the shared `apiClient` so page components
// never call fetch directly. Slice 1 exposes `runSql`; Slice 2 adds the
// schema-listing call (`listSchema`); Slice 4 adds the analysis call
// (`analyze`) that produces a validated declarative Chart_Spec.

import { apiClient, TIMEOUT_LONG_MS } from "./client";
import type { ChartSpec } from "../pages/computeChartSpecData";

/**
 * Analysis mode for `POST /sql/analyze` (Slice 4). `default` maps to the Gemini
 * Flash model on the backend; `deep` maps to the Gemini Pro model.
 */
export type AnalysisMode = "default" | "deep";

/**
 * Timeout for the analysis call, in milliseconds. Matches the backend's 60s
 * language-model budget (design: R9.10) rather than the generic long timeout.
 */
export const TIMEOUT_ANALYZE_MS = 60_000;

/**
 * The result of a successful `POST /sql/run`, mirroring the backend
 * `SqlRunResponse` (design: Result_Set).
 */
export interface ResultSet {
  /** Column names in select order. */
  columns: string[];
  /** At most Row_Limit rows, keyed by column name. */
  rows: Record<string, unknown>[];
  /** Number of rows actually returned. */
  rowCount: number;
  /** Whole-millisecond measured execution time. */
  durationMs: number;
  /** The submitted SQL, echoed back. */
  sql: string;
  /** True iff the query produced more than Row_Limit rows. */
  truncated: boolean;
}

/** Request body for `POST /sql/run`. */
export interface RunSqlRequest {
  sql: string;
}

/** A single column in the schema listing (`GET /sql/schema`). */
export interface SchemaColumn {
  /** Column name. */
  name: string;
  /** Declared column data type (e.g. `text`, `integer`). */
  type: string;
}

/**
 * A table and its columns in the schema listing (`GET /sql/schema`), mirroring
 * the backend `SchemaTableResponse`. Only tables the viewer role holds a
 * `SELECT` grant on are returned, so sensitive tables never appear.
 */
export interface SchemaTable {
  /** Table name. */
  name: string;
  /** Columns in the table. */
  columns: SchemaColumn[];
}

/**
 * Execute a read-only SELECT through `POST /sql/run` and return the Result_Set.
 *
 * Uses the long timeout because a query can run up to the backend's
 * statement-timeout budget. Pass an `AbortSignal` to cancel an in-flight run.
 */
export function runSql(sql: string, signal?: AbortSignal): Promise<ResultSet> {
  const payload: RunSqlRequest = { sql };
  return apiClient.postJson<ResultSet>("/sql/run", payload, {
    timeoutMs: TIMEOUT_LONG_MS,
    signal,
  });
}

/**
 * List the tables and columns the viewer role can `SELECT`, via
 * `GET /sql/schema`. Used to populate the schema sidebar. Pass an
 * `AbortSignal` to cancel an in-flight request (e.g. on unmount).
 */
export function listSchema(signal?: AbortSignal): Promise<SchemaTable[]> {
  return apiClient.get<SchemaTable[]>("/sql/schema", { signal });
}

/**
 * Request an AI auto-dashboard for a Result_Set via `POST /sql/analyze`
 * (Slice 4) and return the validated, strictly declarative `ChartSpec`.
 *
 * The backend forwards only the column names, inferred types, row count, and a
 * bounded row sample to the language model, and the returned spec carries no
 * precomputed numbers — the `AutoDashboard` computes every displayed value
 * locally from the actual rows. Uses a 60s timeout matching the backend's
 * model budget (R9.10). Pass an `AbortSignal` to cancel an in-flight request.
 */
export function analyze(
  result: ResultSet,
  mode: AnalysisMode = "default",
  signal?: AbortSignal,
): Promise<ChartSpec> {
  const payload = { ...result, mode };
  return apiClient.postJson<ChartSpec>("/sql/analyze", payload, {
    timeoutMs: TIMEOUT_ANALYZE_MS,
    signal,
  });
}
