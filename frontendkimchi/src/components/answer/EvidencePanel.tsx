import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, FileText, Database } from "lucide-react";
import type { Claim, DocumentEvidenceItem, DatabaseEvidenceItem, EvidenceItem } from "../../api/types";
import { shortenId } from "../../lib/format";
import styles from "./EvidencePanel.module.css";

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

interface EvidencePanelProps {
  /** The currently selected claim; null when no claim is selected. */
  claim: Claim | null;
}

/**
 * Displays evidence items for a selected `supported` or `partially_supported`
 * claim. On any rendering error, shows an "evidence unavailable" notice while
 * preserving the answer and claims (R1.12).
 */
export function EvidencePanel({ claim }: EvidencePanelProps) {
  if (!claim) return null;

  const showable =
    claim.evidence_status === "supported" ||
    claim.evidence_status === "partially_supported";

  if (!showable) return null;

  return (
    <EvidencePanelErrorBoundary>
      <EvidencePanelContent claim={claim} />
    </EvidencePanelErrorBoundary>
  );
}

// ---------------------------------------------------------------------------
// Internal content renderer
// ---------------------------------------------------------------------------

function EvidencePanelContent({ claim }: { claim: Claim }) {
  const { evidence_items } = claim;

  if (!evidence_items || evidence_items.length === 0) {
    return (
      <div className={styles.panel} role="region" aria-label="Evidence for selected claim">
        <p className={styles.emptyNotice}>No evidence items available for this claim.</p>
      </div>
    );
  }

  return (
    <div className={styles.panel} role="region" aria-label="Evidence for selected claim">
      <div className={styles.panelHeader}>
        <FileText size={16} className={styles.panelHeaderIcon} aria-hidden="true" />
        <span>Evidence ({evidence_items.length})</span>
      </div>
      <div className={styles.evidenceList}>
        {evidence_items.map((item, index) => (
          <EvidenceItemCard key={index} item={item} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Evidence item renderers
// ---------------------------------------------------------------------------

function EvidenceItemCard({ item }: { item: EvidenceItem }) {
  if (item.kind === "document") {
    return <DocumentEvidenceCard item={item} />;
  }
  if (item.kind === "database") {
    return <DatabaseEvidenceCard item={item} />;
  }
  // Unknown kind — defensive fallback
  return (
    <div className={styles.evidenceItem}>
      <span className={styles.evidenceKindBadge}>Unknown evidence type</span>
    </div>
  );
}

function DocumentEvidenceCard({ item }: { item: DocumentEvidenceItem }) {
  return (
    <div className={styles.evidenceItem}>
      <span className={styles.evidenceKindBadge}>
        <FileText size={12} aria-hidden="true" />
        Document
      </span>
      <blockquote className={styles.quote}>{item.quote}</blockquote>
      <div className={styles.sourceMeta}>
        <span>
          <span className={styles.sourceLabel}>Source: </span>
          <span className={styles.sourceValue}>{shortenId(item.document_id)}</span>
        </span>
        <span>
          <span className={styles.sourceLabel}>Version: </span>
          <span className={styles.sourceValue}>{shortenId(item.document_version)}</span>
        </span>
      </div>
    </div>
  );
}

function DatabaseEvidenceCard({ item }: { item: DatabaseEvidenceItem }) {
  const entries = Object.entries(item.row_fields ?? {});
  return (
    <div className={styles.evidenceItem}>
      <span className={styles.evidenceKindBadge}>
        <Database size={12} aria-hidden="true" />
        Database
      </span>
      <span className={styles.tableName}>{item.table}</span>
      {entries.length > 0 && (
        <table className={styles.rowFields}>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Value</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([field, value]) => (
              <tr key={field}>
                <th scope="row">{field}</th>
                <td>{String(value ?? "—")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Error boundary — shows "evidence unavailable" without crashing the answer
// ---------------------------------------------------------------------------

interface ErrorBoundaryState {
  hasError: boolean;
}

class EvidencePanelErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Log for diagnostics but don't propagate — preserves answer + claims.
    console.error("[EvidencePanel] Render error caught:", error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className={styles.unavailableNotice} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>Evidence unavailable — the answer and claims are preserved above.</span>
        </div>
      );
    }
    return this.props.children;
  }
}
