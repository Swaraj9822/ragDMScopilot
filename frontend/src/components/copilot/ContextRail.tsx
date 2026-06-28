import { Link } from "react-router-dom";
import { Files, X } from "lucide-react";
import { shortenId } from "../../lib/format";
import styles from "./ContextRail.module.css";

interface ContextRailProps {
  selectedIds: string[];
  onRemove: (id: string) => void;
  historyCount: number;
  onNewSession: () => void;
}

export function ContextRail({
  selectedIds,
  onRemove,
  historyCount,
  onNewSession,
}: ContextRailProps) {
  return (
    <aside className={styles.rail} aria-label="Query context">
      <section className={styles.block}>
        <h2 className={styles.heading}>Selected documents</h2>
        {selectedIds.length === 0 ? (
          <p className={styles.empty}>
            No documents selected. The copilot searches the full corpus.
          </p>
        ) : (
          <ul className={styles.chips}>
            {selectedIds.map((id) => (
              <li key={id} className={styles.chip}>
                <span className="mono">{shortenId(id)}</span>
                <button
                  type="button"
                  onClick={() => onRemove(id)}
                  aria-label={`Remove document ${id}`}
                >
                  <X size={12} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
        <Link to="/documents" className={`btn btn-sm ${styles.docLink}`}>
          <Files size={14} aria-hidden="true" />
          Manage documents
        </Link>
      </section>

      <section className={styles.block}>
        <h2 className={styles.heading}>Local history</h2>
        <p className={styles.empty}>
          {historyCount === 0
            ? "Questions in this browser session appear here. The backend does not store conversation history."
            : `${historyCount} question${historyCount === 1 ? "" : "s"} saved in this browser.`}
        </p>
        <button
          type="button"
          className="btn btn-sm"
          onClick={onNewSession}
          disabled={historyCount === 0}
        >
          New session
        </button>
      </section>
    </aside>
  );
}
