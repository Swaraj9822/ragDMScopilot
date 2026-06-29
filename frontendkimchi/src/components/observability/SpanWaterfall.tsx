import { useMemo } from "react";
import type { Span } from "../../api/types";
import { computeWaterfall, cappedDepth } from "../../lib/waterfall";
import { formatDuration } from "../../lib/format";
import styles from "./SpanWaterfall.module.css";

interface SpanWaterfallProps {
  spans: Span[];
  selectedSpanId: string | null;
  onSelect: (spanId: string) => void;
}

const MIN_WIDTH_PCT = 0.6; // visual minimum so tiny spans stay visible

export function SpanWaterfall({ spans, selectedSpanId, onSelect }: SpanWaterfallProps) {
  const layout = useMemo(() => computeWaterfall(spans), [spans]);

  if (layout.rows.length === 0) {
    return <p className="meta">No spans in this trace.</p>;
  }

  const total = layout.totalMs;
  const ticks = makeTicks(total);

  return (
    <div className={styles.waterfall}>
      {/* Accessible semantic table equivalent of the visual waterfall. */}
      <table className="visually-hidden">
        <caption>Span timing</caption>
        <thead>
          <tr>
            <th>Operation</th>
            <th>Start (ms)</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Depth</th>
          </tr>
        </thead>
        <tbody>
          {layout.rows.map((row) => (
            <tr key={row.span.span_id}>
              <td>{row.span.operation}</td>
              <td>{Math.round(row.startMs)}</td>
              <td>{formatDuration(row.span.duration_ms)}</td>
              <td>{row.span.status}</td>
              <td>{row.depth}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className={styles.ruler} aria-hidden="true">
        {ticks.map((tick) => (
          <span
            key={tick}
            className={styles.tick}
            style={{ left: `${total > 0 ? (tick / total) * 100 : 0}%` }}
          >
            {Math.round(tick)} ms
          </span>
        ))}
      </div>

      <ul className={styles.rows}>
        {layout.rows.map((row) => {
          const selected = row.span.span_id === selectedSpanId;
          const widthPct = Math.max(MIN_WIDTH_PCT, row.width * 100);
          const offsetPct = Math.min(row.offset * 100, 100 - MIN_WIDTH_PCT);
          return (
            <li key={row.span.span_id} className={styles.row}>
              <button
                type="button"
                className={`${styles.rowButton} ${selected ? styles.selected : ""}`}
                onClick={() => onSelect(row.span.span_id)}
                aria-pressed={selected}
                style={{ paddingLeft: `${cappedDepth(row.depth) * 14}px` }}
              >
                <span className={styles.label} title={row.span.operation}>
                  {row.span.operation}
                </span>
                <span className={styles.track}>
                  <span
                    className={`${styles.bar} ${
                      row.span.status === "error" ? styles.barError : styles.barOk
                    } ${selected ? styles.barSelected : ""}`}
                    style={{ left: `${offsetPct}%`, width: `${widthPct}%` }}
                  />
                </span>
                <span className={styles.duration}>
                  {formatDuration(row.span.duration_ms)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function makeTicks(total: number): number[] {
  if (total <= 0) return [0];
  const count = 4;
  return Array.from({ length: count + 1 }, (_, i) => (total / count) * i);
}
