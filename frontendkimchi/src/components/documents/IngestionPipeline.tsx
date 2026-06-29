import { Check, CheckCircle2, Loader2, X } from "lucide-react";
import { PIPELINE_STEPS } from "../../lib/constants";
import type { DocumentStatus } from "../../api/types";
import styles from "./IngestionPipeline.module.css";

// Map an API status to the index of the active step (0-based) and whether the
// pipeline has completed or failed.
const STATUS_TO_INDEX: Record<string, number> = {
  queued: 0,
  parsing: 1,
  chunking: 2,
  embedding: 3,
  indexed: 4,
};

interface IngestionPipelineProps {
  status: DocumentStatus | string;
}

export function IngestionPipeline({ status }: IngestionPipelineProps) {
  const failed = status === "failed";
  const deleted = status === "deleted";
  const indexed = status === "indexed";
  const activeIndex = STATUS_TO_INDEX[status] ?? 0;

  if (deleted) {
    return <p className={styles.deleted}>Document deleted.</p>;
  }

  return (
    <div>
      <ol className={styles.pipeline} aria-label="Ingestion progress">
        {PIPELINE_STEPS.map((step, i) => {
          const complete = indexed ? true : i < activeIndex;
          const active = !indexed && i === activeIndex;
          const isFailedStep = failed && i === activeIndex;
          const state = isFailedStep
            ? "failed"
            : complete
              ? "complete"
              : active
                ? "active"
                : "pending";
          return (
            <li key={step} className={`${styles.step} ${styles[state]}`}>
              <span className={styles.marker} aria-hidden="true">
                {state === "complete" && <Check size={12} />}
                {state === "active" && <Loader2 size={12} className={styles.spin} />}
                {state === "failed" && <X size={12} />}
              </span>
              <span className={styles.label}>{step}</span>
            </li>
          );
        })}
      </ol>
      {indexed && (
        <p className={styles.done}>
          <CheckCircle2 size={14} aria-hidden="true" className={styles.doneIcon} />
          Indexed in Pinecone
        </p>
      )}
    </div>
  );
}
