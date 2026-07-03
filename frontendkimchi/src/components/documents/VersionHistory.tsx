import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, XCircle, History, Circle } from "lucide-react";
import { fetchDocumentHistory, restoreDocumentVersion } from "../../api/documents";
import { ApiError } from "../../api/client";
import { useAuth } from "../../hooks/useAuth";
import { ConfirmDialog } from "../common/ConfirmDialog";
import { ErrorState } from "../common/ErrorState";
import { EmptyState } from "../common/EmptyState";
import { Skeleton } from "../common/Skeleton";
import { RelativeTime } from "../common/RelativeTime";
import type { DocumentVersion, IngestionEvent } from "../../api/types";
import styles from "./VersionHistory.module.css";

interface VersionHistoryProps {
  documentId: string;
}

/**
 * Displays the version history for a document, including versions and ingestion
 * events ordered newest-first. Operators can restore a non-active version via a
 * confirmation dialog.
 *
 * Requirements: 5.7 (history listing), 5.8 (restore action).
 */
export function VersionHistory({ documentId }: VersionHistoryProps) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const isOperator = user?.is_operator ?? false;

  const {
    data: history,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ["documentHistory", documentId],
    queryFn: () => fetchDocumentHistory(documentId),
  });

  const [restoreTarget, setRestoreTarget] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  const handleRestore = useCallback(async () => {
    if (!restoreTarget) return;
    setRestoring(true);
    setRestoreError(null);
    try {
      await restoreDocumentVersion(documentId, restoreTarget);
      setRestoreTarget(null);
      // Refetch the history so the UI reflects the new active version.
      await queryClient.invalidateQueries({ queryKey: ["documentHistory", documentId] });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setRestoreError("Version not found. It may have been removed.");
      } else if (err instanceof ApiError) {
        setRestoreError(err.detail);
      } else {
        setRestoreError("Failed to restore version. Please try again.");
      }
    } finally {
      setRestoring(false);
    }
  }, [documentId, restoreTarget, queryClient]);

  if (isLoading) {
    return (
      <div className={styles.container} aria-busy="true" aria-label="Loading version history">
        <Skeleton height={56} />
        <Skeleton height={56} />
        <Skeleton height={56} />
      </div>
    );
  }

  if (error) {
    const message =
      error instanceof ApiError ? error.detail : "Could not load version history.";
    return (
      <ErrorState
        title="Failed to load version history"
        body={message}
        action={
          <button type="button" className="btn btn-primary" onClick={() => refetch()}>
            Retry
          </button>
        }
      />
    );
  }

  if (!history || (history.versions.length === 0 && history.events.length === 0)) {
    return <EmptyState icon={History} title="No version history" body="This document has no recorded versions or ingestion events." />;
  }

  // Merge versions and events into a single timeline, newest-first.
  // Both are already sorted newest-first from the backend.
  const timelineEntries = buildTimeline(history.versions, history.events, history.active_version);

  return (
    <div className={styles.container}>
      <ul className={styles.timeline} aria-label="Version history">
        {timelineEntries.map((entry) => (
          <li
            key={entry.key}
            className={`${styles.entry} ${entry.isActive ? styles.entryActive : ""}`}
          >
            <span className={styles.icon}>
              {entry.type === "version" && entry.isActive && (
                <CheckCircle size={18} className={styles.iconActive} aria-hidden="true" />
              )}
              {entry.type === "version" && !entry.isActive && (
                <Circle size={18} className={styles.iconSuccess} aria-hidden="true" />
              )}
              {entry.type === "event" && entry.status === "succeeded" && (
                <CheckCircle size={18} className={styles.iconSuccess} aria-hidden="true" />
              )}
              {entry.type === "event" && entry.status === "failed" && (
                <XCircle size={18} className={styles.iconFailed} aria-hidden="true" />
              )}
            </span>

            <div className={styles.details}>
              <span className={styles.versionLabel}>
                {entry.type === "version" ? `Version ${entry.version}` : `Ingestion ${entry.version}`}
                {entry.isActive && <span className={styles.activeBadge}>Active</span>}
              </span>
              <span className={styles.meta}>
                <RelativeTime iso={entry.timestamp} />
                {entry.type === "event" && (
                  <span>· {entry.status === "succeeded" ? "Succeeded" : "Failed"}</span>
                )}
                {entry.type === "version" && entry.indexed && <span>· Indexed</span>}
              </span>
              {entry.type === "event" && entry.error && (
                <span className={styles.eventError}>{entry.error}</span>
              )}
            </div>

            {entry.type === "version" && !entry.isActive && isOperator && (
              <div className={styles.actions}>
                <button
                  type="button"
                  className={styles.restoreBtn}
                  onClick={() => {
                    setRestoreError(null);
                    setRestoreTarget(entry.version);
                  }}
                  aria-label={`Restore version ${entry.version}`}
                >
                  Restore
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      {restoreError && (
        <p className={styles.errorNotice} role="alert">
          {restoreError}
        </p>
      )}

      <ConfirmDialog
        open={restoreTarget !== null}
        title="Restore version?"
        body={`This will set version "${restoreTarget ?? ""}" as the active version for this document. Retrieval will use this version going forward.`}
        confirmLabel="Restore"
        cancelLabel="Cancel"
        busy={restoring}
        onConfirm={handleRestore}
        onCancel={() => {
          setRestoreTarget(null);
          setRestoreError(null);
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

interface TimelineVersionEntry {
  key: string;
  type: "version";
  version: string;
  timestamp: string;
  isActive: boolean;
  indexed: boolean;
}

interface TimelineEventEntry {
  key: string;
  type: "event";
  version: string;
  timestamp: string;
  isActive: false;
  status: string;
  error: string | null;
}

type TimelineEntry = TimelineVersionEntry | TimelineEventEntry;

/**
 * Merge versions and events into a single chronological list (newest-first).
 * Both inputs are already sorted newest-first from the backend (R5.7).
 */
function buildTimeline(
  versions: DocumentVersion[],
  events: IngestionEvent[],
  activeVersion: string | null,
): TimelineEntry[] {
  const versionEntries: TimelineEntry[] = versions.map((v) => ({
    key: `version-${v.version}`,
    type: "version" as const,
    version: v.version,
    timestamp: v.created_at,
    isActive: v.version === activeVersion,
    indexed: v.indexed,
  }));

  const eventEntries: TimelineEntry[] = events.map((e) => ({
    key: `event-${e.ingestion_id}`,
    type: "event" as const,
    version: e.version,
    timestamp: e.timestamp,
    isActive: false as const,
    status: e.status,
    error: e.error,
  }));

  // Merge both lists preserving newest-first order.
  const merged: TimelineEntry[] = [];
  let vi = 0;
  let ei = 0;
  while (vi < versionEntries.length && ei < eventEntries.length) {
    const vTime = new Date(versionEntries[vi].timestamp).getTime();
    const eTime = new Date(eventEntries[ei].timestamp).getTime();
    if (vTime >= eTime) {
      merged.push(versionEntries[vi]);
      vi++;
    } else {
      merged.push(eventEntries[ei]);
      ei++;
    }
  }
  while (vi < versionEntries.length) {
    merged.push(versionEntries[vi]);
    vi++;
  }
  while (ei < eventEntries.length) {
    merged.push(eventEntries[ei]);
    ei++;
  }

  return merged;
}
