import { useMemo } from "react";
import { Info } from "lucide-react";
import type { Trace } from "../../api/types";
import { computeSummary, routeDistribution } from "../../lib/observability";
import { formatDuration } from "../../lib/format";
import { routeLabel } from "../../lib/status";
import styles from "./SummaryStrip.module.css";

interface SummaryStripProps {
  traces: Trace[];
  hideConsoleTraffic: boolean;
  onToggleConsoleTraffic: (value: boolean) => void;
  atLimit: boolean;
}

const ROUTE_COLORS = ["#43d6b5", "#79a9ff", "#e9ad55", "#ff716c", "#5bd29a", "#b07cff"];

export function SummaryStrip({
  traces,
  hideConsoleTraffic,
  onToggleConsoleTraffic,
  atLimit,
}: SummaryStripProps) {
  const summary = useMemo(() => computeSummary(traces), [traces]);
  const distribution = useMemo(() => routeDistribution(traces), [traces]);

  return (
    <section className={styles.strip} aria-label="Loaded window summary">
      <div className={styles.cards}>
        <SummaryCard label="Traces" value={String(summary.count)} hint="Loaded window" />
        <SummaryCard
          label="Error rate"
          value={`${(summary.errorRate * 100).toFixed(1)}%`}
          tone={summary.errorRate > 0 ? "warning" : "default"}
        />
        <SummaryCard
          label="p95 duration"
          value={summary.p95Ms != null ? formatDuration(summary.p95Ms) : "—"}
        />
        <SummaryCard
          label="Slowest route"
          value={summary.slowestRoute ? routeLabel(summary.slowestRoute) : "—"}
          mono={!!summary.slowestRoute}
        />
      </div>

      {distribution.length > 0 && (
        <div
          className={styles.distBar}
          role="img"
          aria-label={`Route distribution: ${distribution
            .map((d) => `${d.route} ${Math.round(d.fraction * 100)}%`)
            .join(", ")}`}
        >
          {distribution.map((slice, i) => (
            <span
              key={slice.route}
              className={styles.distSlice}
              style={{
                width: `${slice.fraction * 100}%`,
                background: ROUTE_COLORS[i % ROUTE_COLORS.length],
              }}
              title={`${slice.route}: ${slice.count}`}
            />
          ))}
        </div>
      )}

      <div className={styles.footer}>
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={hideConsoleTraffic}
            onChange={(e) => onToggleConsoleTraffic(e.target.checked)}
          />
          Hide console traffic
          <span
            className={styles.infoIcon}
            title="Hides /health, /traces, /logs, and /metrics requests made by this console from the list and summary. Turn off to diagnose the observability endpoints themselves."
          >
            <Info size={13} aria-hidden="true" />
          </span>
        </label>
        {atLimit && (
          <span className="meta">Based on the latest 500 matching traces</span>
        )}
      </div>
    </section>
  );
}

function SummaryCard({
  label,
  value,
  hint,
  tone = "default",
  mono,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "warning";
  mono?: boolean;
}) {
  return (
    <div className={styles.card}>
      <span className={styles.cardLabel}>{label}</span>
      <span
        className={`${styles.cardValue} ${tone === "warning" ? styles.warn : ""} ${mono ? "mono" : ""}`}
      >
        {value}
      </span>
      {hint && <span className={styles.cardHint}>{hint}</span>}
    </div>
  );
}
