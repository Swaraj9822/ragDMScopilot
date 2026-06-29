import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { LogRecord } from "../../api/types";
import { statusTone, TONE_COLOR_VAR } from "../../lib/status";
import { formatAbsolute, shortenId } from "../../lib/format";
import { KeyValueList } from "../common/KeyValueList";
import styles from "./LogList.module.css";

interface LogListProps {
  logs: LogRecord[];
  /** Optional click handler for trace ids (e.g. jump to trace view). */
  onTraceClick?: (traceId: string) => void;
  showTraceId?: boolean;
}

export function LogList({ logs, onTraceClick, showTraceId = true }: LogListProps) {
  return (
    <ul className={styles.list}>
      {logs.map((log, i) => (
        <LogRow
          key={`${log.insertion_seq}-${i}`}
          log={log}
          onTraceClick={onTraceClick}
          showTraceId={showTraceId}
        />
      ))}
    </ul>
  );
}

function LogRow({
  log,
  onTraceClick,
  showTraceId,
}: {
  log: LogRecord;
  onTraceClick?: (traceId: string) => void;
  showTraceId: boolean;
}) {
  const [open, setOpen] = useState(false);
  const hasDetail = !!log.exc_text || Object.keys(log.extra).length > 0;
  const levelColor = `var(${TONE_COLOR_VAR[statusTone(log.level)]})`;

  return (
    <li className={styles.row}>
      <div className={styles.head}>
        {hasDetail ? (
          <button
            type="button"
            className={styles.expander}
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-label={open ? "Collapse log detail" : "Expand log detail"}
          >
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        ) : (
          <span className={styles.expanderSpacer} />
        )}
        <time className={styles.time} title={formatAbsolute(log.timestamp)}>
          {formatAbsolute(log.timestamp)}
        </time>
        <span
          className={styles.level}
          style={{ color: levelColor, borderColor: levelColor }}
        >
          {log.level}
        </span>
        <span className={styles.logger} title={log.logger}>
          {log.logger}
        </span>
        <span className={styles.message}>{log.message}</span>
        {showTraceId && log.trace_id && (
          <span className={styles.trace}>
            {onTraceClick ? (
              <button
                type="button"
                className={styles.traceLink}
                onClick={() => onTraceClick(log.trace_id!)}
              >
                {shortenId(log.trace_id, 8, 4)}
              </button>
            ) : (
              <span className="mono">{shortenId(log.trace_id, 8, 4)}</span>
            )}
          </span>
        )}
      </div>
      {open && hasDetail && (
        <div className={styles.detail}>
          {log.exc_text && <pre className={styles.exc}>{log.exc_text}</pre>}
          {Object.keys(log.extra).length > 0 && <KeyValueList data={log.extra} />}
        </div>
      )}
    </li>
  );
}
