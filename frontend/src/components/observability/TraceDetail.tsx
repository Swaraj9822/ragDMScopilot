import { useEffect, useRef, useState } from "react";
import { ScrollText } from "lucide-react";
import type { Trace } from "../../api/types";
import { StatusBadge } from "../common/StatusBadge";
import { CopyButton } from "../common/CopyButton";
import { RouteBadge } from "../common/RouteBadge";
import { formatAbsolute, formatDuration, shortenId } from "../../lib/format";
import { SpanWaterfall } from "./SpanWaterfall";
import { SpanInspector } from "./SpanInspector";
import { CorrelatedLogs } from "./CorrelatedLogs";
import styles from "./TraceDetail.module.css";

interface TraceDetailProps {
  trace: Trace;
}

export function TraceDetail({ trace }: TraceDetailProps) {
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(
    trace.spans[0]?.span_id ?? null,
  );
  const logsRef = useRef<HTMLDivElement>(null);

  // Switching to a different trace must reset the selected span so we never
  // keep a stale selection (or accidentally match an unrelated span id).
  useEffect(() => {
    setSelectedSpanId(trace.spans[0]?.span_id ?? null);
  }, [trace.trace_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedSpan =
    trace.spans.find((s) => s.span_id === selectedSpanId) ?? trace.spans[0] ?? null;

  return (
    <div className={styles.detail}>
      <header className={styles.header}>
        <div className={styles.headerTop}>
          <RouteBadge route={trace.route} />
          <StatusBadge status={trace.root_status} label={trace.root_status} />
          <span className={styles.metaItem}>{formatDuration(trace.duration_ms)}</span>
          <span className={styles.metaItem}>{trace.spans.length} spans</span>
        </div>
        <div className={styles.headerBottom}>
          <span className="meta" title={formatAbsolute(trace.start_ts)}>
            {formatAbsolute(trace.start_ts)}
          </span>
          <span className={styles.traceId}>
            <span className="mono">{shortenId(trace.trace_id, 12, 6)}</span>
            <CopyButton value={trace.trace_id} label="Copy trace ID" iconOnly />
          </span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => logsRef.current?.focus()}
          >
            <ScrollText size={14} aria-hidden="true" />
            Open logs
          </button>
        </div>
      </header>

      <section className={styles.block}>
        <h3 className={styles.blockTitle}>Span waterfall</h3>
        <SpanWaterfall
          spans={trace.spans}
          selectedSpanId={selectedSpanId}
          onSelect={setSelectedSpanId}
        />
      </section>

      {selectedSpan && (
        <section className={styles.block}>
          <h3 className={styles.blockTitle}>Span inspector</h3>
          <SpanInspector span={selectedSpan} />
        </section>
      )}

      <CorrelatedLogs traceId={trace.trace_id} ref={logsRef} />
    </div>
  );
}
