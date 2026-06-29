import { AlertCircle, Check, FileText, Loader2, X } from "lucide-react";
import type { UploadTask } from "../../hooks/useUploadManager";
import styles from "./UploadQueue.module.css";

interface UploadQueueProps {
  tasks: UploadTask[];
  onRemove: (id: string) => void;
  onClearFinished: () => void;
}

const STATE_LABEL: Record<UploadTask["state"], string> = {
  invalid: "Invalid",
  queued: "Queued",
  uploading: "Uploading…",
  uploaded: "Accepted",
  error: "Failed",
};

export function UploadQueue({ tasks, onRemove, onClearFinished }: UploadQueueProps) {
  if (tasks.length === 0) return null;

  const hasFinished = tasks.some(
    (t) => t.state === "uploaded" || t.state === "error" || t.state === "invalid",
  );

  return (
    <section className={styles.wrap} aria-label="Upload queue">
      <div className={styles.head}>
        <h3 className={styles.title}>Upload queue</h3>
        {hasFinished && (
          <button type="button" className="btn btn-sm" onClick={onClearFinished}>
            Clear finished
          </button>
        )}
      </div>
      <ul className={styles.list}>
        {tasks.map((task) => (
          <li key={task.id} className={styles.item}>
            <span className={styles.icon} aria-hidden="true">
              {task.state === "uploading" && <Loader2 size={16} className={styles.spin} />}
              {task.state === "uploaded" && <Check size={16} className={styles.ok} />}
              {(task.state === "error" || task.state === "invalid") && (
                <AlertCircle size={16} className={styles.fail} />
              )}
              {task.state === "queued" && <FileText size={16} />}
            </span>
            <div className={styles.info}>
              <span className={styles.name} title={task.fileName}>
                {task.fileName}
              </span>
              <span className="meta">
                {task.sizeLabel} · {STATE_LABEL[task.state]}
                {task.documentId ? ` · ${task.documentId.slice(0, 8)}…` : ""}
              </span>
              {task.error && <span className={styles.errorText}>{task.error}</span>}
            </div>
            {task.state !== "uploading" && (
              <button
                type="button"
                className={styles.remove}
                onClick={() => onRemove(task.id)}
                aria-label={`Remove ${task.fileName} from queue`}
              >
                <X size={14} aria-hidden="true" />
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
