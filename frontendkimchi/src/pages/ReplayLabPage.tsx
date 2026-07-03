import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  FlaskConical,
  Loader2,
  Play,
  Square,
  GitCompareArrows,
} from "lucide-react";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import {
  createReplayRun,
  getReplayRun,
  cancelReplayRun,
  listCorpusSnapshots,
  type CorpusSnapshotSummary,
} from "../api/replays";
import { ApiError } from "../api/client";
import type {
  ReplayRun,
  ReplayRunRequest,
  ReplayRunResult,
  EvidenceItem,
} from "../api/types";
import styles from "./ReplayLabPage.module.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);
const POLL_INTERVAL_MS = 3_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function stateClassName(state: string): string {
  switch (state) {
    case "queued":
      return styles.stateQueued;
    case "running":
      return styles.stateRunning;
    case "completed":
      return styles.stateCompleted;
    case "failed":
      return styles.stateFailed;
    case "cancelled":
      return styles.stateCancelled;
    default:
      return "";
  }
}

function formatEvidence(item: EvidenceItem): string {
  if (item.kind === "document") {
    return item.quote.length > 80 ? `${item.quote.slice(0, 80)}…` : item.quote;
  }
  return `[${item.table}] ${JSON.stringify(item.row_fields).slice(0, 80)}`;
}

// ---------------------------------------------------------------------------
// ComparisonView
// ---------------------------------------------------------------------------

interface ComparisonViewProps {
  runA: ReplayRun;
  runB: ReplayRun;
}

function MetricRow({
  label,
  valueA,
  valueB,
  format,
  lowerIsBetter,
}: {
  label: string;
  valueA: number;
  valueB: number;
  format?: (v: number) => string;
  lowerIsBetter?: boolean;
}) {
  const fmt = format ?? ((v: number) => String(v));
  const diff = valueB - valueA;
  const better = lowerIsBetter ? diff < 0 : diff > 0;
  const worse = lowerIsBetter ? diff > 0 : diff < 0;
  return (
    <div className={styles.metricRow}>
      <span className={styles.metricLabel}>{label}</span>
      <span>
        <span className={styles.metricValue}>{fmt(valueA)}</span>
        {" vs "}
        <span className={styles.metricValue}>{fmt(valueB)}</span>
        {diff !== 0 && (
          <span
            className={`${styles.metricDiff} ${better ? styles.metricBetter : worse ? styles.metricWorse : ""}`}
          >
            {diff > 0 ? "+" : ""}
            {fmt(diff)}
          </span>
        )}
      </span>
    </div>
  );
}

export function ComparisonView({ runA, runB }: ComparisonViewProps) {
  const resultA = runA.result as ReplayRunResult;
  const resultB = runB.result as ReplayRunResult;

  return (
    <section className={styles.comparisonSection} aria-label="Run comparison">
      <div className={styles.comparisonHeader}>
        <h2 style={{ fontSize: 18, fontWeight: 600 }}>
          <GitCompareArrows size={18} aria-hidden="true" style={{ marginRight: 8, verticalAlign: "middle" }} />
          Side-by-Side Comparison
        </h2>
      </div>

      {/* Metrics comparison */}
      <MetricRow
        label="Route"
        valueA={0}
        valueB={0}
        format={() => ""}
      />
      <div className={styles.metricRow}>
        <span className={styles.metricLabel}>Route</span>
        <span>
          <span className={styles.metricValue}>{resultA.route}</span>
          {" vs "}
          <span className={styles.metricValue}>{resultB.route}</span>
        </span>
      </div>
      <MetricRow
        label="Latency (ms)"
        valueA={resultA.latency_ms}
        valueB={resultB.latency_ms}
        format={(v) => `${v.toLocaleString()}ms`}
        lowerIsBetter
      />
      <MetricRow
        label="Prompt tokens"
        valueA={resultA.prompt_tokens}
        valueB={resultB.prompt_tokens}
        format={(v) => v.toLocaleString()}
        lowerIsBetter
      />
      <MetricRow
        label="Completion tokens"
        valueA={resultA.completion_tokens}
        valueB={resultB.completion_tokens}
        format={(v) => v.toLocaleString()}
        lowerIsBetter
      />
      <MetricRow
        label="Cost"
        valueA={resultA.cost}
        valueB={resultB.cost}
        format={(v) => `$${v.toFixed(4)}`}
        lowerIsBetter
      />

      {/* Side-by-side columns */}
      <div className={styles.comparisonGrid}>
        <ComparisonColumn label="Run A" result={resultA} run={runA} />
        <ComparisonColumn label="Run B" result={resultB} run={runB} />
      </div>
    </section>
  );
}

function ComparisonColumn({
  label,
  result,
  run,
}: {
  label: string;
  result: ReplayRunResult;
  run: ReplayRun;
}) {
  const avgScore =
    result.retrieval_scores.length > 0
      ? result.retrieval_scores.reduce((a, b) => a + b, 0) /
        result.retrieval_scores.length
      : 0;

  return (
    <div className={styles.comparisonColumn}>
      <div className={styles.comparisonColumnTitle}>
        {label} — {run.request.question.slice(0, 50)}
        {run.request.question.length > 50 ? "…" : ""}
      </div>

      <div className={styles.metricRow}>
        <span className={styles.metricLabel}>Route</span>
        <span className={styles.metricValue}>{result.route}</span>
      </div>

      <div className={styles.metricRow}>
        <span className={styles.metricLabel}>Avg retrieval score</span>
        <span className={styles.metricValue}>{avgScore.toFixed(3)}</span>
      </div>

      <div className={styles.metricRow}>
        <span className={styles.metricLabel}>Latency</span>
        <span className={styles.metricValue}>{result.latency_ms.toLocaleString()}ms</span>
      </div>

      <div className={styles.metricRow}>
        <span className={styles.metricLabel}>Tokens</span>
        <span className={styles.metricValue}>
          {result.prompt_tokens.toLocaleString()} / {result.completion_tokens.toLocaleString()}
        </span>
      </div>

      <div className={styles.metricRow}>
        <span className={styles.metricLabel}>Cost</span>
        <span className={styles.metricValue}>${result.cost.toFixed(4)}</span>
      </div>

      {/* Answer */}
      <div>
        <span className={styles.metricLabel}>Answer</span>
        <div className={styles.answerBlock}>{result.answer}</div>
      </div>

      {/* Evidence */}
      <div>
        <span className={styles.metricLabel}>Evidence ({result.evidence.length})</span>
        <div className={styles.evidenceList}>
          {result.evidence.length === 0 && (
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>No evidence</span>
          )}
          {result.evidence.map((item, i) => (
            <div key={i} className={styles.evidenceChip}>
              {formatEvidence(item)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ReplayLabPage
// ---------------------------------------------------------------------------

export default function ReplayLabPage() {
  // Form state
  const [question, setQuestion] = useState("");
  const [configVersionId, setConfigVersionId] = useState("");
  const [corpusSnapshotId, setCorpusSnapshotId] = useState("");
  const [maxPassages, setMaxPassages] = useState(10);
  const [minScore, setMinScore] = useState(0.3);

  // Runs state
  const [runs, setRuns] = useState<ReplayRun[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Corpus snapshots
  const [snapshots, setSnapshots] = useState<CorpusSnapshotSummary[]>([]);
  const [loadingSnapshots, setLoadingSnapshots] = useState(true);

  // Polling
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Load available snapshots on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await listCorpusSnapshots();
        if (!cancelled) {
          setSnapshots(list);
          if (list.length > 0 && !corpusSnapshotId) {
            setCorpusSnapshotId(list[0].corpus_snapshot_id);
          }
        }
      } catch {
        // Non-critical; operator can still type the id manually
      } finally {
        if (!cancelled) setLoadingSnapshots(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Polling logic for non-terminal runs
  const pollRuns = useCallback(async () => {
    const nonTerminal = runs.filter((r) => !TERMINAL_STATES.has(r.state));
    if (nonTerminal.length === 0) return;

    const updates = await Promise.allSettled(
      nonTerminal.map((r) => getReplayRun(r.replay_run_id)),
    );

    if (!mountedRef.current) return;

    setRuns((prev) => {
      const map = new Map(prev.map((r) => [r.replay_run_id, r]));
      updates.forEach((result, i) => {
        if (result.status === "fulfilled") {
          map.set(nonTerminal[i].replay_run_id, result.value);
        }
      });
      return Array.from(map.values());
    });
  }, [runs]);

  // Start/stop polling when there are non-terminal runs
  useEffect(() => {
    const hasActive = runs.some((r) => !TERMINAL_STATES.has(r.state));
    if (hasActive) {
      if (!pollTimerRef.current) {
        pollTimerRef.current = setInterval(pollRuns, POLL_INTERVAL_MS);
      }
    } else {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    }
    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [runs, pollRuns]);

  // Initiate a run
  async function handleInitiate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const payload: ReplayRunRequest = {
      question: question.trim(),
      ai_configuration_version_id: configVersionId.trim(),
      retrieval_params: {
        max_passages: maxPassages,
        min_score: minScore,
      },
      corpus_snapshot_id: corpusSnapshotId.trim(),
    };

    try {
      const run = await createReplayRun(payload);
      setRuns((prev) => [run, ...prev]);
      setQuestion("");
    } catch (err) {
      const message =
        err instanceof ApiError ? err.detail : "Failed to initiate replay run.";
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }

  // Cancel a run
  async function handleCancel(runId: string) {
    try {
      await cancelReplayRun(runId);
      // Optimistically update
      setRuns((prev) =>
        prev.map((r) =>
          r.replay_run_id === runId
            ? { ...r, state: "cancelled", cancel_requested: true }
            : r,
        ),
      );
    } catch {
      // Ignore cancel errors — next poll will reflect actual state
    }
  }

  // Toggle selection for comparison
  function toggleSelection(runId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) {
        next.delete(runId);
      } else {
        // Allow max 2 selections
        if (next.size >= 2) {
          // Replace the first selected
          const [first] = next;
          next.delete(first);
        }
        next.add(runId);
      }
      return next;
    });
  }

  // Get the two selected completed runs for comparison
  const selectedRuns = runs.filter((r) => selectedIds.has(r.replay_run_id));
  const canCompare =
    selectedRuns.length === 2 &&
    selectedRuns.every((r) => r.state === "completed" && r.result !== null);

  return (
    <div className={styles.layout}>
      <PageHeader
        title="Replay & Compare Lab"
        subtitle="Replay a question under different configurations and compare results side by side."
      />

      {/* ── Initiate form ── */}
      <form className={styles.initiateForm} onSubmit={handleInitiate}>
        <div className={`${styles.formField} ${styles.fullWidth}`}>
          <label htmlFor="replay-question">Question</label>
          <input
            id="replay-question"
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Enter the question to replay…"
            required
          />
        </div>

        <div className={styles.formField}>
          <label htmlFor="replay-config-version">AI Config Version ID</label>
          <input
            id="replay-config-version"
            type="text"
            value={configVersionId}
            onChange={(e) => setConfigVersionId(e.target.value)}
            placeholder="Approved config version ID"
            required
          />
        </div>

        <div className={styles.formField}>
          <label htmlFor="replay-snapshot">Corpus Snapshot</label>
          {loadingSnapshots ? (
            <input type="text" value="" readOnly disabled placeholder="Loading snapshots…" />
          ) : snapshots.length > 0 ? (
            <select
              id="replay-snapshot"
              value={corpusSnapshotId}
              onChange={(e) => setCorpusSnapshotId(e.target.value)}
              required
            >
              <option value="" disabled>
                Select a snapshot…
              </option>
              {snapshots.map((s) => (
                <option key={s.corpus_snapshot_id} value={s.corpus_snapshot_id}>
                  {s.corpus_snapshot_id.slice(0, 8)}… ({s.manifest_size} docs,{" "}
                  {new Date(s.created_at).toLocaleDateString()})
                </option>
              ))}
            </select>
          ) : (
            <input
              id="replay-snapshot"
              type="text"
              value={corpusSnapshotId}
              onChange={(e) => setCorpusSnapshotId(e.target.value)}
              placeholder="Corpus snapshot ID"
              required
            />
          )}
        </div>

        <div className={styles.formField}>
          <label htmlFor="replay-max-passages">Max Passages (1–100)</label>
          <input
            id="replay-max-passages"
            type="number"
            min={1}
            max={100}
            value={maxPassages}
            onChange={(e) => setMaxPassages(Number(e.target.value))}
            required
          />
        </div>

        <div className={styles.formField}>
          <label htmlFor="replay-min-score">Min Score (0.00–1.00)</label>
          <input
            id="replay-min-score"
            type="number"
            min={0}
            max={1}
            step={0.01}
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
            required
          />
        </div>

        <div className={styles.formActions}>
          {error && (
            <span style={{ color: "var(--danger)", fontSize: 13, marginRight: "auto" }}>
              <AlertTriangle size={14} style={{ verticalAlign: "middle", marginRight: 4 }} aria-hidden="true" />
              {error}
            </span>
          )}
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting ? (
              <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
            ) : (
              <Play size={14} aria-hidden="true" />
            )}
            {submitting ? "Starting…" : "Start Replay"}
          </button>
        </div>
      </form>

      {/* ── Runs list ── */}
      <div>
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: "var(--space-3)" }}>
          Runs
        </h2>
        {selectedIds.size > 0 && (
          <p style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: "var(--space-2)" }}>
            Select exactly 2 completed runs to compare.
            {canCompare && " Ready to compare!"}
          </p>
        )}

        {runs.length === 0 ? (
          <EmptyState
            icon={FlaskConical}
            title="No replay runs yet."
            body="Start a replay run above to see results here."
          />
        ) : (
          <div className={styles.runsList} role="list" aria-label="Replay runs">
            {runs.map((run) => {
              const isSelected = selectedIds.has(run.replay_run_id);
              const isCompleted = run.state === "completed";
              const isActive = !TERMINAL_STATES.has(run.state);

              return (
                <div
                  key={run.replay_run_id}
                  className={`${styles.runCard} ${isSelected ? styles.selected : ""}`}
                  role="listitem"
                >
                  {/* Checkbox for comparison selection */}
                  <input
                    type="checkbox"
                    className={styles.checkbox}
                    checked={isSelected}
                    disabled={!isCompleted && !isSelected}
                    onChange={() => toggleSelection(run.replay_run_id)}
                    aria-label={`Select run for comparison: ${run.request.question.slice(0, 40)}`}
                  />

                  <div className={styles.runInfo}>
                    <div className={styles.runQuestion}>{run.request.question}</div>
                    <div className={styles.runMeta}>
                      <span className={`${styles.stateBadge} ${stateClassName(run.state)}`}>
                        {run.state}
                      </span>
                      <span>Config: {run.request.ai_configuration_version_id.slice(0, 8)}…</span>
                      {run.failure_reason && (
                        <span style={{ color: "var(--danger)" }}>{run.failure_reason}</span>
                      )}
                    </div>
                  </div>

                  <div className={styles.runActions}>
                    {isActive && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => handleCancel(run.replay_run_id)}
                        aria-label={`Cancel run: ${run.request.question.slice(0, 30)}`}
                      >
                        <Square size={12} aria-hidden="true" />
                        Cancel
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ── Comparison ── */}
      {canCompare ? (
        <ComparisonView runA={selectedRuns[0]} runB={selectedRuns[1]} />
      ) : selectedIds.size > 0 && !canCompare ? (
        <div className={styles.selectHint}>
          Select exactly 2 <strong>completed</strong> runs to view a side-by-side comparison.
        </div>
      ) : null}
    </div>
  );
}
