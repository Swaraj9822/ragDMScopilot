import { Link } from "react-router-dom";
import { Eraser, Files, MessageSquarePlus, X } from "lucide-react";
import { shortenId } from "../../lib/format";
import styles from "./ContextRail.module.css";

interface ContextRailProps {
  selectedIds: string[];
  onRemove: (id: string) => void;
  historyCount: number;
  conversationId: string | null;
  onNewTopic: () => void;
  onForgetContext: () => void;
  busy?: boolean;
}

export function ContextRail({
  selectedIds,
  onRemove,
  historyCount,
  conversationId,
  onNewTopic,
  onForgetContext,
  busy = false,
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
        <h2 className={styles.heading}>Conversation</h2>
        <p className={styles.empty}>
          {conversationId
            ? "The copilot remembers this thread, so follow-ups can build on earlier questions."
            : "Ask a question to start a conversation. Follow-ups will build on it automatically."}
        </p>
        {conversationId && (
          <p className={styles.convoId}>
            <span className={styles.convoLabel}>Thread</span>
            <span className="mono">{shortenId(conversationId)}</span>
          </p>
        )}
        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-sm"
            onClick={onForgetContext}
            disabled={busy || !conversationId}
            title="Keep this thread but stop follow-ups from referencing earlier turns"
          >
            <Eraser size={14} aria-hidden="true" />
            Forget context
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={onNewTopic}
            disabled={busy || (historyCount === 0 && selectedIds.length === 0 && !conversationId)}
            title="Start a brand-new conversation"
          >
            <MessageSquarePlus size={14} aria-hidden="true" />
            Start new topic
          </button>
        </div>
      </section>
    </aside>
  );
}
