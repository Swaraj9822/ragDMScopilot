import { AlertTriangle } from "lucide-react";
import type { Span } from "../../api/types";
import { StatusBadge } from "../common/StatusBadge";
import { CopyButton } from "../common/CopyButton";
import { KeyValueList } from "../common/KeyValueList";
import { formatAbsolute, formatDuration, shortenId } from "../../lib/format";
import styles from "./SpanInspector.module.css";

interface SpanInspectorProps {
  span: Span;
}

const EXCEPTION_KEYS = ["exception.type", "exception.message", "exception_type", "exception_message"];

export function SpanInspector({ span }: SpanInspectorProps) {
  const exceptionEntries = Object.entries(span.attributes).filter(([k]) =>
    EXCEPTION_KEYS.includes(k),
  );
  const otherAttributes = Object.fromEntries(
    Object.entries(span.attributes).filter(([k]) => !EXCEPTION_KEYS.includes(k)),
  );

  return (
    <div className={styles.inspector}>
      <div className={styles.header}>
        <span className={styles.operation}>{span.operation}</span>
        <StatusBadge status={span.status} label={span.status} />
      </div>

      <dl className={styles.fields}>
        <Field label="Start">{formatAbsolute(span.start_ts)}</Field>
        <Field label="Duration">{formatDuration(span.duration_ms)}</Field>
        <Field label="Span ID">
          <span className="mono">{shortenId(span.span_id, 10, 6)}</span>
          <CopyButton value={span.span_id} label="Copy span ID" iconOnly />
        </Field>
        <Field label="Parent">
          {span.parent_span_id ? (
            <span className="mono">{shortenId(span.parent_span_id, 10, 6)}</span>
          ) : (
            <span className="meta">root</span>
          )}
        </Field>
      </dl>

      {exceptionEntries.length > 0 && (
        <div className={styles.exception} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            {exceptionEntries.map(([key, value]) => (
              <div key={key}>
                <span className={styles.excKey}>{key}</span>: {String(value)}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className={styles.attributes}>
        <h4 className={styles.attrTitle}>Attributes</h4>
        {Object.keys(otherAttributes).length === 0 ? (
          <p className="meta">No attributes.</p>
        ) : (
          <KeyValueList data={otherAttributes} />
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.fieldLabel}>{label}</dt>
      <dd className={styles.fieldValue}>{children}</dd>
    </div>
  );
}
