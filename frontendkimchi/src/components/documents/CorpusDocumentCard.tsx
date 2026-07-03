import { FileText } from "lucide-react";
import type { DocumentRecord } from "../../api/types";
import { shortenId } from "../../lib/format";
import { StatusBadge } from "../common/StatusBadge";
import { CopyButton } from "../common/CopyButton";
import styles from "./DocumentCard.module.css";

interface CorpusDocumentCardProps {
  document: DocumentRecord;
}

/**
 * Read-only card for a document from the server corpus listing (R4.10).
 * Renders each returned document independent of browser-local state.
 */
export function CorpusDocumentCard({ document: doc }: CorpusDocumentCardProps) {
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
        {doc.owner && (
          <span className="meta">· owner: {doc.owner}</span>
        )}
      </div>
    </article>
  );
}
