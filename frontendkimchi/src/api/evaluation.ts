import { apiClient, TIMEOUT_LONG_MS, TIMEOUT_SHORT_MS } from "./client";
import type { BenchmarkResult } from "./types";

// ---------------------------------------------------------------------------
// Evaluation (R7)
// ---------------------------------------------------------------------------

/** Shape returned by `GET /evaluation/runs`. */
export interface EvaluationRunSummary {
  run_id: string;
  created_at: string;
  ci_passed: boolean;
  result_count: number;
}

/** Shape returned by `GET /evaluation/runs/:runId`. */
export interface EvaluationRunDetail {
  run_id: string;
  created_at: string;
  ci_passed: boolean;
  results: BenchmarkResult[];
}

/** List evaluation runs (most recent first). */
export function listEvaluationRuns(): Promise<EvaluationRunSummary[]> {
  return apiClient.get<EvaluationRunSummary[]>("/evaluation/runs", {
    timeoutMs: TIMEOUT_SHORT_MS,
  });
}

/** Get the full detail of an evaluation run, including per-case results. */
export function getEvaluationRun(runId: string): Promise<EvaluationRunDetail> {
  return apiClient.get<EvaluationRunDetail>(
    `/evaluation/runs/${encodeURIComponent(runId)}`,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

/** Trigger a new deterministic evaluation run over the default set (operator-only). */
export function runEvaluation(): Promise<EvaluationRunDetail> {
  return apiClient.postJson<EvaluationRunDetail>(
    "/evaluation/runs",
    {},
    { timeoutMs: TIMEOUT_LONG_MS },
  );
}
