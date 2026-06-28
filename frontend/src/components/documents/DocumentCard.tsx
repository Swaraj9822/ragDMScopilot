import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Activity, FileText, MessageSquareText, RefreshCw, Replace, Trash2 } from "lucide-react";
import type { BrowserDocumentEntry } from "../../api/types";
import { ACCEPT_ATTR } from "../../lib/constants";
import { shortenId } from "../../lib/format";
import { StatusBadge } from "../common/StatusBadge";
import { CopyButton } from "../common/CopyButton";
import { RelativeTime } from "../common/RelativeTime";
import { IngestionPipeline } from "./IngestionPipeline";
import { useDocumentPolling } from "../../hooks/useDocumentPolling";
import { useSelectedDocuments } from "../../hooks/useSelectedDocuments";
import { useToast } from "../../hooks/useToast";
import styles from "./DocumentCard.module.css";

const TERMINAL = new Set(["indexed", "failed", "deleted"]);

interface DocumentCardProps {
  entry: BrowserDocumentEntry;
  onReplace: (documentId: string, file: File) => void;
  onDelete: (entry: BrowserDocumentEntry) => void;
  onRemoveLocal: (documentId: string) => void;
}

export function DocumentCard({
  entry,
  onReplace,
  onDelete,
  onRemoveLocal,
}: DocumentCardProps) {
  const navigate = useNavigate();
  const { add } = useSelectedDocuments();
  const { pushToast } = useToast();
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const [traceMessage, setTraceMessage] = useState<string | null>(null);

  const doc = entry.document;
  const isTerminal = TERMINAL.has(doc.status);
  const notFound = doc.status === "deleted" && doc.error === "Not found";

  const { isChecking, pollingTimedOut, checkNow } = useDocumentPolling({
    documentId: doc.id,
    initialStatus: doc.status,
    enabled: !isTerminal,
  });

  function handleUseInCopilot() {
    add(doc.id);
    pushToast("Added to Copilot selection", "success");
    navigate("/copilot");
  }

  function handleReplacePick(file: File | undefined) {
    if (file) onReplace(doc.id, file);
  }

  return (
    <article className={styles.card}>
      <div className={styles.header}>
        <div className={styles.title}>
          <FileText size={16} aria-hidden="true" className={styles.fileIcon} />
          <span title={doc.title}>{doc.title}</span>
        </div>
        <StatusBadge status={doc.status} label={doc.status} />
      </div>

      <div className={styles.idRow}>
        <span className="mono meta">{shortenId(doc.id, 10, 6)}</span>
        <CopyButton value={doc.id} label="Copy ID" iconOnly />
        <span className="meta">v{doc.version}</span>
        <span className="meta">
          · updated <RelativeTime iso={entry.last_checked_at} />
        </span>
      </div>

      {notFound ? (
        <p className={styles.notFound}>
          Not found on the server. It may have been deleted elsewhere.
        </p>
      ) : (
        <IngestionPipeline status={doc.status} />
      )}

      {doc.status === "failed" && doc.error && (
        <p className={styles.error} role="alert">
          {doc.error}
        </p>
      )}

      {pollingTimedOut && (
        <button type="button" className="btn btn-sm" onClick={() => checkNow()}>
          <RefreshCw size={14} aria-hidden="true" />
          Check status
        </button>
      )}

      {traceMessage && <p className={styles.traceNote}>{traceMessage}</p>}

      <div className={styles.actions}>
        <button type="button" className="btn btn-sm" onClick={handleUseInCopilot}>
          <MessageSquareText size={14} aria-hidden="true" />
          Use in Copilot
        </button>

        <button
          type="button"
          className="btn btn-sm"
          onClick={() => replaceInputRef.current?.click()}
        >
          <Replace size={14} aria-hidden="true" />
          Replace file
        </button>
        <input
          ref={replaceInputRef}
          type="file"
          accept={ACCEPT_ATTR}
          className="visually-hidden"
          aria-label={`Replace ${doc.title}`}
          onChange={(e) => {
            handleReplacePick(e.target.files?.[0]);
            e.target.value = "";
          }}
        />

        {entry.request_trace_id && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              setTraceMessage(null);
              navigate(`/observability?trace=${entry.request_trace_id}`);
            }}
          >
            <Activity size={14} aria-hidden="true" />
            Inspect trace
          </button>
        )}

        {isChecking && <span className="meta">Checking…</span>}

        <span className={styles.spacer} />

        {notFound ? (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => onRemoveLocal(doc.id)}
          >
            Remove from history
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-sm btn-danger"
            onClick={() => onDelete(entry)}
          >
            <Trash2 size={14} aria-hidden="true" />
            Delete
          </button>
        )}
      </div>
    </article>
  );
}
