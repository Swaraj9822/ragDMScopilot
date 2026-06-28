import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity, Terminal, RefreshCw, Search } from "lucide-react";
import { api, type Trace, type LogEntry } from "../api";
import styles from "./Observability.module.css";

type View = "traces" | "logs";
type Preset = "15m" | "1h" | "6h" | "24h";
const PRESETS: { value: Preset; label: string }[] = [
  { value: "15m", label: "15m" }, { value: "1h", label: "1h" }, { value: "6h", label: "6h" }, { value: "24h", label: "24h" },
];
const MS: Record<Preset, number> = { "15m": 15*60e3, "1h": 60*60e3, "6h": 6*60*60e3, "24h": 24*60*60e3 };

export default function Observability() {
  const [params, setParams] = useSearchParams();
  const view: View = params.get("view") === "logs" ? "logs" : "traces";
  const selectedId = params.get("trace");
  const [preset, setPreset] = useState<Preset>("1h");
  const [statusFilter, setStatusFilter] = useState<"" | "success" | "error">("");

  const now = useMemo(() => new Date().toISOString(), [preset]); // eslint-disable-line
  const start = useMemo(() => new Date(Date.now() - MS[preset]).toISOString(), [preset]);

  const tracesQ = useQuery({
    queryKey: ["traces", preset, statusFilter],
    queryFn: () => api.searchTraces({ start, end: now, status: statusFilter || undefined, limit: 200 }),
    enabled: view === "traces",
    refetchInterval: 10_000,
  });

  const logsQ = useQuery({
    queryKey: ["logs"],
    queryFn: () => api.searchLogs({ limit: 200 }),
    enabled: view === "logs",
    refetchInterval: 10_000,
  });

  const selectedTrace = useMemo(() => {
    if (!selectedId) return null;
    return (tracesQ.data ?? []).find((t) => t.trace_id === selectedId) ?? null;
  }, [selectedId, tracesQ.data]);

  const deepQ = useQuery({
    queryKey: ["trace", selectedId],
    queryFn: () => api.getTrace(selectedId!),
    enabled: !!selectedId && !selectedTrace,
  });

  const traceDetail = selectedTrace ?? deepQ.data ?? null;

  function setView(v: View) { setParams((p) => { const n = new URLSearchParams(p); n.set("view", v); return n; }); }
  function selectTrace(id: string) { setParams((p) => { const n = new URLSearchParams(p); n.set("trace", id); n.set("view", "traces"); return n; }, { replace: true }); }

  const traces = tracesQ.data ?? [];
  const errorRate = traces.length ? traces.filter((t) => t.root_status === "error").length / traces.length : 0;
  const p95 = traces.length ? [...traces].sort((a, b) => a.duration_ms - b.duration_ms)[Math.floor(traces.length * 0.95)]?.duration_ms ?? 0 : 0;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Observability</h1>
        <button className="btn btn-ghost btn-icon" onClick={() => tracesQ.refetch()} type="button" aria-label="Refresh">
          <RefreshCw size={15} />
        </button>
      </div>

      {/* View switcher */}
      <div className={styles.switcher}>
        <button className={`${styles.switchTab} ${view === "traces" ? styles.switchActive : ""}`} onClick={() => setView("traces")} type="button">
          <Activity size={14} /> Traces
        </button>
        <button className={`${styles.switchTab} ${view === "logs" ? styles.switchActive : ""}`} onClick={() => setView("logs")} type="button">
          <Terminal size={14} /> Logs
        </button>
      </div>

      {view === "traces" && (
        <>
          {/* Stats row */}
          <div className={styles.stats}>
            <Stat label="Traces" value={String(traces.length)} />
            <Stat label="Error rate" value={`${(errorRate * 100).toFixed(1)}%`} danger={errorRate > 0.05} />
            <Stat label="P95" value={p95 > 0 ? `${p95.toFixed(0)}ms` : "—"} />
          </div>

          {/* Filters */}
          <div className={styles.filters}>
            <div className={styles.presets}>
              {PRESETS.map((p) => (
                <button key={p.value} className={`${styles.preset} ${preset === p.value ? styles.presetActive : ""}`} onClick={() => setPreset(p.value)} type="button">
                  {p.label}
                </button>
              ))}
            </div>
            <select className="select" style={{ width: 100, height: 30 }} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)} aria-label="Status filter">
              <option value="">All</option>
              <option value="success">Success</option>
              <option value="error">Error</option>
            </select>
          </div>

          {/* Trace list + detail */}
          <div className={styles.masterDetail}>
            <div className={styles.list}>
              {tracesQ.isLoading && <p className={styles.placeholder}>Loading traces…</p>}
              {tracesQ.isError && <p className={styles.placeholder} style={{ color: "var(--danger)" }}>Failed to load traces</p>}
              {traces.length === 0 && !tracesQ.isLoading && <p className={styles.placeholder}>No traces in this window</p>}
              {traces.map((t) => (
                <button
                  key={t.trace_id}
                  className={`${styles.row} ${selectedId === t.trace_id ? styles.rowSelected : ""}`}
                  onClick={() => selectTrace(t.trace_id)}
                  type="button"
                >
                  <span className={`${styles.statusDot} ${t.root_status === "error" ? styles.dotError : styles.dotOk}`} />
                  <span className={styles.rowRoute}>{t.route}</span>
                  <span className={styles.rowDur}>{t.duration_ms}ms</span>
                  <span className={styles.rowTime}>{new Date(t.start_ts).toLocaleTimeString()}</span>
                </button>
              ))}
            </div>

            <div className={styles.detail}>
              {traceDetail ? (
                <TraceDetail trace={traceDetail} />
              ) : (
                <div className={styles.detailEmpty}>
                  <Search size={20} style={{ color: "var(--ink-muted)" }} />
                  <p>Select a trace to inspect</p>
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {view === "logs" && (
        <div className={styles.logTable}>
          <div className={styles.logHead}>
            <span>Time</span><span>Level</span><span>Message</span><span>Trace</span>
          </div>
          {logsQ.isLoading && <p className={styles.placeholder}>Loading logs…</p>}
          {(logsQ.data ?? []).slice(0, 100).map((log, i) => (
            <LogRow key={`${log.timestamp}-${i}`} log={log} onTrace={selectTrace} />
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue} style={danger ? { color: "var(--danger)" } : undefined}>{value}</span>
    </div>
  );
}

function TraceDetail({ trace }: { trace: Trace }) {
  const sorted = [...trace.spans].sort((a, b) => new Date(a.start_ts).getTime() - new Date(b.start_ts).getTime());
  const origin = sorted.length ? new Date(sorted[0].start_ts).getTime() : 0;
  const total = trace.duration_ms || 1;

  return (
    <div className={styles.detailContent}>
      <div className={styles.detailHead}>
        <h3 className={styles.detailTitle}>{trace.trace_id.slice(0, 12)}…</h3>
        <span className={`pill ${trace.root_status === "error" ? styles.dangerPill : styles.okPill}`}>{trace.root_status}</span>
        <span className={`pill ${styles.routePillObs}`}>{trace.route}</span>
        <span className={styles.detailDur}>{trace.duration_ms}ms</span>
      </div>
      <div className={styles.waterfall}>
        {sorted.map((span) => {
          const start = new Date(span.start_ts).getTime() - origin;
          const left = (start / total) * 100;
          const width = Math.max((span.duration_ms / total) * 100, 0.5);
          return (
            <div key={span.span_id} className={styles.wfRow}>
              <span className={styles.wfLabel}>{span.operation}</span>
              <div className={styles.wfTrack}>
                <div
                  className={styles.wfBar}
                  style={{
                    left: `${left}%`, width: `${width}%`,
                    background: span.status === "error" ? "var(--danger)" : "var(--primary)",
                  }}
                />
              </div>
              <span className={styles.wfDur}>{span.duration_ms}ms</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LogRow({ log, onTrace }: { log: LogEntry; onTrace: (id: string) => void }) {
  const levelColor = log.level === "ERROR" || log.level === "CRITICAL" ? "var(--danger)" : log.level === "WARNING" ? "var(--warning)" : "var(--ink-muted)";
  return (
    <div className={styles.logRow}>
      <span className={styles.logTime}>{new Date(log.timestamp).toLocaleTimeString()}</span>
      <span className={styles.logLevel} style={{ color: levelColor }}>{log.level}</span>
      <span className={styles.logMsg}>{log.message}</span>
      {log.trace_id ? (
        <button className={styles.logTrace} onClick={() => onTrace(log.trace_id!)} type="button">{log.trace_id.slice(0, 8)}…</button>
      ) : <span />}
    </div>
  );
}
