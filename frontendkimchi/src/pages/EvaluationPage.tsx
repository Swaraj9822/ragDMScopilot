import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Gauge,
  XCircle,
  BarChart3,
  Brain,
  Play,
} from "lucide-react";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import { ErrorState } from "../components/common/ErrorState";
import { Skeleton } from "../components/common/Skeleton";
import { ApiError } from "../api/client";
import {
  listEvaluationRuns,
  getEvaluationRun,
  runEvaluation,
  type EvaluationRunSummary,
  type EvaluationRunDetail,
} from "../api/evaluation";
import type {
  BenchmarkResult,
  DeterministicCheck,
  RetrievalMetrics,
  LLMJudgeScores,
} from "../api/types";
import styles from "./EvaluationPage.module.css";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function scoreColor(score: number): string {
  if (score >= 0.8) return "var(--success, #16a34a)";
  if (score >= 0.5) return "var(--warning, #d97706)";
  return "var(--danger, #dc2626)";
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function CIStatusBanner({ passed }: { passed: boolean }) {
  return (
    <div
      className={`${styles.ciBanner} ${passed ? styles.ciPassed : styles.ciFailed}`}
      role="status"
      aria-label={`CI status: ${passed ? "passed" : "failed"}`}
    >
      {passed ? (
        <CheckCircle2 size={18} aria-hidden="true" />
      ) : (
        <XCircle size={18} aria-hidden="true" />
      )}
      <span>
        CI Status: {passed ? "Passed" : "Failed"}
        {!passed && " — one or more deterministic checks failed"}
      </span>
    </div>
  );
}

function CheckBadge({ check }: { check: DeterministicCheck }) {
  const passed = check.outcome === "pass";
  return (
    <span
      className={`${styles.checkBadge} ${passed ? styles.checkPass : styles.checkFail}`}
      title={`${check.name}: ${check.outcome}`}
    >
      {passed ? (
        <CheckCircle2 size={11} aria-hidden="true" />
      ) : (
        <XCircle size={11} aria-hidden="true" />
      )}
      {check.name}
    </span>
  );
}

function ScoreBar({ score, label }: { score: number; label: string }) {
  const pct = Math.round(score * 100);
  const color = scoreColor(score);
  return (
    <span className={styles.scoreBar} title={`${label}: ${pct}%`}>
      <span className={styles.metricValue}>{pct}%</span>
      <span className={styles.scoreBarTrack}>
        <span
          className={styles.scoreBarFill}
          style={{ width: `${pct}%`, background: color }}
        />
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Method sections
// ---------------------------------------------------------------------------

function DeterministicSection({ results }: { results: BenchmarkResult[] }) {
  return (
    <section className={styles.methodSection} aria-labelledby="section-deterministic">
      <h2 id="section-deterministic" className={styles.methodTitle}>
        <ClipboardCheck size={18} aria-hidden="true" />
        Deterministic Checks
      </h2>
      <table className={styles.resultsTable} aria-label="Deterministic check results">
        <thead>
          <tr>
            <th scope="col">Case ID</th>
            <th scope="col">Checks</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.case_id}>
              <td>
                <span className={styles.metricValue}>{r.case_id}</span>
              </td>
              <td>
                {r.deterministic_checks.length > 0 ? (
                  <div className={styles.checkList}>
                    {r.deterministic_checks.map((check, i) => (
                      <CheckBadge key={i} check={check} />
                    ))}
                  </div>
                ) : (
                  <span className={styles.noDataHint}>No checks</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function RetrievalSection({ results }: { results: BenchmarkResult[] }) {
  const withMetrics = results.filter(
    (r): r is BenchmarkResult & { retrieval_metrics: RetrievalMetrics } =>
      r.retrieval_metrics != null,
  );

  if (withMetrics.length === 0) {
    return (
      <section className={styles.methodSection} aria-labelledby="section-retrieval">
        <h2 id="section-retrieval" className={styles.methodTitle}>
          <BarChart3 size={18} aria-hidden="true" />
          Retrieval Metrics
        </h2>
        <p className={styles.noDataHint}>
          No retrieval metrics available. Cases require relevance labels to compute these metrics.
        </p>
      </section>
    );
  }

  return (
    <section className={styles.methodSection} aria-labelledby="section-retrieval">
      <h2 id="section-retrieval" className={styles.methodTitle}>
        <BarChart3 size={18} aria-hidden="true" />
        Retrieval Metrics
      </h2>
      <table className={styles.resultsTable} aria-label="Retrieval metric results">
        <thead>
          <tr>
            <th scope="col">Case ID</th>
            <th scope="col">Recall@k</th>
            <th scope="col">Precision@k</th>
            <th scope="col">MRR@k</th>
            <th scope="col">Depth</th>
          </tr>
        </thead>
        <tbody>
          {withMetrics.map((r) => (
            <tr key={r.case_id}>
              <td>
                <span className={styles.metricValue}>{r.case_id}</span>
              </td>
              <td>
                <ScoreBar score={r.retrieval_metrics.recall_at_k} label="Recall" />
              </td>
              <td>
                <ScoreBar score={r.retrieval_metrics.precision_at_k} label="Precision" />
              </td>
              <td>
                <ScoreBar score={r.retrieval_metrics.mrr_at_k} label="MRR" />
              </td>
              <td>
                <span className={styles.metricValue}>{r.retrieval_metrics.depth}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function LLMJudgeSection({ results }: { results: BenchmarkResult[] }) {
  const withJudge = results.filter(
    (r): r is BenchmarkResult & { llm_judge: LLMJudgeScores } =>
      r.llm_judge != null,
  );

  if (withJudge.length === 0) {
    return (
      <section className={styles.methodSection} aria-labelledby="section-llm-judge">
        <h2 id="section-llm-judge" className={styles.methodTitle}>
          <Brain size={18} aria-hidden="true" />
          LLM Judge Scores
        </h2>
        <p className={styles.noDataHint}>
          No LLM judge scores available. LLM scoring runs on a scheduled interval and is excluded from CI.
        </p>
      </section>
    );
  }

  return (
    <section className={styles.methodSection} aria-labelledby="section-llm-judge">
      <h2 id="section-llm-judge" className={styles.methodTitle}>
        <Brain size={18} aria-hidden="true" />
        LLM Judge Scores
      </h2>
      <table className={styles.resultsTable} aria-label="LLM judge score results">
        <thead>
          <tr>
            <th scope="col">Case ID</th>
            <th scope="col">Faithfulness</th>
            <th scope="col">Relevance</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {withJudge.map((r) => (
            <tr key={r.case_id}>
              <td>
                <span className={styles.metricValue}>{r.case_id}</span>
              </td>
              <td>
                {r.llm_judge.faithfulness != null ? (
                  <ScoreBar score={r.llm_judge.faithfulness} label="Faithfulness" />
                ) : (
                  <span className={styles.noDataHint}>—</span>
                )}
              </td>
              <td>
                {r.llm_judge.relevance != null ? (
                  <ScoreBar score={r.llm_judge.relevance} label="Relevance" />
                ) : (
                  <span className={styles.noDataHint}>—</span>
                )}
              </td>
              <td>
                {r.llm_judge.error ? (
                  <span className={styles.errorIndication}>
                    <AlertTriangle size={13} aria-hidden="true" />
                    {r.llm_judge.error}
                  </span>
                ) : (
                  <span style={{ color: "var(--success, #16a34a)", fontSize: 12 }}>
                    <CheckCircle2
                      size={13}
                      aria-hidden="true"
                      style={{ verticalAlign: "middle", marginRight: 4 }}
                    />
                    OK
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// ---------------------------------------------------------------------------
// EvaluationPage
// ---------------------------------------------------------------------------

export default function EvaluationPage() {
  const [runs, setRuns] = useState<EvaluationRunSummary[]>([]);
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<EvaluationRunDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  async function handleRunEvaluation() {
    setRunning(true);
    setRunError(null);
    try {
      const detail = await runEvaluation();
      // Refresh the run list, then surface the freshly created run.
      const list = await listEvaluationRuns();
      setRuns(list);
      setSelectedRunId(detail.run_id);
      setRunDetail(detail);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.detail : "Failed to start an evaluation run.";
      setRunError(message);
    } finally {
      setRunning(false);
    }
  }

  const runAction = (
    <div className={styles.runAction}>
      {runError && (
        <span className={styles.runError} role="alert">
          {runError}
        </span>
      )}
      <button
        type="button"
        className={styles.runButton}
        onClick={handleRunEvaluation}
        disabled={running}
        aria-busy={running}
      >
        <Play size={15} aria-hidden="true" />
        {running ? "Running…" : "Run evaluation"}
      </button>
    </div>
  );

  // Load available runs on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await listEvaluationRuns();
        if (cancelled) return;
        setRuns(list);
        if (list.length > 0) {
          setSelectedRunId(list[0].run_id);
        }
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiError ? err.detail : "Failed to load evaluation runs.";
        setRunsError(message);
      } finally {
        if (!cancelled) setLoadingRuns(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Load detail when selection changes
  useEffect(() => {
    if (!selectedRunId) {
      setRunDetail(null);
      return;
    }

    let cancelled = false;
    setLoadingDetail(true);
    setDetailError(null);

    (async () => {
      try {
        const detail = await getEvaluationRun(selectedRunId);
        if (cancelled) return;
        setRunDetail(detail);
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiError ? err.detail : "Failed to load run details.";
        setDetailError(message);
      } finally {
        if (!cancelled) setLoadingDetail(false);
      }
    })();

    return () => { cancelled = true; };
  }, [selectedRunId]);

  // Loading state
  if (loadingRuns) {
    return (
      <div className={styles.layout}>
        <PageHeader
          title="Evaluation Dashboard"
          subtitle="Multi-method evaluation results by run."
        />
        <div aria-busy="true">
          <Skeleton height={36} />
          <Skeleton height={200} />
        </div>
      </div>
    );
  }

  // Error loading runs
  if (runsError) {
    return (
      <div className={styles.layout}>
        <PageHeader
          title="Evaluation Dashboard"
          subtitle="Multi-method evaluation results by run."
        />
        <ErrorState title="Could not load evaluation runs" body={runsError} />
      </div>
    );
  }

  // No runs at all
  if (runs.length === 0) {
    return (
      <div className={styles.layout}>
        <PageHeader
          title="Evaluation Dashboard"
          subtitle="Multi-method evaluation results by run."
          actions={runAction}
        />
        <EmptyState
          icon={Gauge}
          title="No evaluation runs yet"
          body="Trigger an evaluation run to see results here. The evaluation set must include at least one human-reviewed benchmark case."
        />
      </div>
    );
  }

  return (
    <div className={styles.layout}>
      <PageHeader
        title="Evaluation Dashboard"
        subtitle="Multi-method evaluation results by run."
        actions={runAction}
      />

      {/* Run selector */}
      <div className={styles.runSelector}>
        <label htmlFor="eval-run-select">Evaluation Run</label>
        <select
          id="eval-run-select"
          value={selectedRunId ?? ""}
          onChange={(e) => setSelectedRunId(e.target.value)}
        >
          {runs.map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {formatDate(run.created_at)} — {run.result_count} case
              {run.result_count !== 1 ? "s" : ""} —{" "}
              {run.ci_passed ? "CI Passed" : "CI Failed"}
            </option>
          ))}
        </select>
      </div>

      {/* Detail loading */}
      {loadingDetail && (
        <div aria-busy="true">
          <Skeleton height={40} />
          <Skeleton height={180} />
          <Skeleton height={180} />
        </div>
      )}

      {/* Detail error */}
      {detailError && !loadingDetail && (
        <ErrorState title="Could not load run details" body={detailError} />
      )}

      {/* Run detail */}
      {runDetail && !loadingDetail && !detailError && (
        <>
          {/* CI Status */}
          <CIStatusBanner passed={runDetail.ci_passed} />

          {/* Deterministic checks */}
          <DeterministicSection results={runDetail.results} />

          {/* Retrieval metrics */}
          <RetrievalSection results={runDetail.results} />

          {/* LLM Judge */}
          <LLMJudgeSection results={runDetail.results} />
        </>
      )}
    </div>
  );
}
