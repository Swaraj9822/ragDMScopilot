import type { Trace } from "../../api/types";
import { formatDuration, shortenId } from "../../lib/format";
import { formatAbsolute, formatRelative } from "../../lib/format";
import { StatusBadge } from "../common/StatusBadge";
import { routeLabel } from "../../lib/status";
import styles from "./TraceList.module.css";

interface TraceListProps {
  traces: Trace[];
  selectedId: string | null;
  onSelect: (traceId: string) => void;
}

export function TraceList({ traces, selectedId, onSelect }: TraceListProps) {
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col">Route</th>
            <th scope="col">Started</th>
            <th scope="col">Duration</th>
            <th scope="col">Spans</th>
            <th scope="col">Trace ID</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((trace) => {
            const selected = trace.trace_id === selectedId;
            return (
              <tr
                key={trace.trace_id}
                className={selected ? styles.selected : undefined}
                aria-selected={selected}
                tabIndex={0}
                onClick={() => onSelect(trace.trace_id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(trace.trace_id);
                  }
                }}
              >
                <td>
                  <StatusBadge status={trace.root_status} label={trace.root_status} />
                </td>
                <td className="mono">{routeLabel(trace.route)}</td>
                <td title={formatAbsolute(trace.start_ts)}>
                  {formatRelative(trace.start_ts)}
                </td>
                <td className="mono">{formatDuration(trace.duration_ms)}</td>
                <td className="mono">{trace.spans.length}</td>
                <td className="mono" title={trace.trace_id}>
                  {shortenId(trace.trace_id, 8, 4)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
