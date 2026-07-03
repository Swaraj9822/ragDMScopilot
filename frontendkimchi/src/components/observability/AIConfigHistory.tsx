import { useCallback, useEffect, useRef, useState } from "react";
import { History, RotateCcw } from "lucide-react";
import type { AIConfigurationVersion } from "../../api/types";
import {
  getAIConfigHistory,
  rollbackAIConfig,
  type AIConfigHistoryResponse,
} from "../../api/aiConfig";
import { ApiError } from "../../api/client";
import { useToast } from "../../hooks/useToast";
import { formatAbsolute, formatRelative, shortenId } from "../../lib/format";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import styles from "./AIConfigHistory.module.css";

interface AIConfigHistoryProps {
  configId: string;
}

const MAX_REASON_LENGTH = 500;

/**
 * Displays AI configuration version history in reverse chronological order
 * with a rollback action and reason capture dialog.
 *
 * Requirements: 9.5, 9.8
 */
export function AIConfigHistory({ configId }: AIConfigHistoryProps) {
  const { pushToast } = useToast();
  const [data, setData] = useState<AIConfigHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Rollback dialog state
  const [rollbackTarget, setRollbackTarget] = useState<AIConfigurationVersion | null>(
    null,
  );
  const [reason, setReason] = useState("");
  const [rolling, setRolling] = useState(false);

  const fetchHistory = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getAIConfigHistory(configId);
      setData(result);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.detail : "Failed to load configuration history";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [configId]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  const openRollbackDialog = (version: AIConfigurationVersion) => {
    setRollbackTarget(version);
    setReason("");
  };

  const closeRollbackDialog = () => {
    if (rolling) return;
    setRollbackTarget(null);
    setReason("");
  };

  const confirmRollback = async () => {
    if (!rollbackTarget || !reason.trim() || reason.length > MAX_REASON_LENGTH) return;
    setRolling(true);
    try {
      await rollbackAIConfig(configId, {
        version_id: rollbackTarget.version_id,
        reason: reason.trim(),
      });
      pushToast(
        `Rolled back to version ${shortenId(rollbackTarget.version_id, 8, 4)}`,
        "success",
      );
      setRollbackTarget(null);
      setReason("");
      // Refresh history to reflect the new active version
      await fetchHistory();
    } catch (err) {
      const message =
        err instanceof ApiError ? err.detail : "Rollback failed";
      pushToast(message, "error");
    } finally {
      setRolling(false);
    }
  };

  if (loading && !data) {
    return (
      <div className={styles.container}>
        <p aria-live="polite">Loading configuration history…</p>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className={styles.container}>
        <ErrorState
          title="Unable to load history"
          body={error}
          action={
            <button type="button" className="btn btn-primary" onClick={fetchHistory}>
              Retry
            </button>
          }
        />
      </div>
    );
  }

  // R9.6: return empty history when no versions exist
  if (!data || data.versions.length === 0) {
    return (
      <div className={styles.container}>
        <EmptyState
          icon={History}
          title="No configuration history"
          body="Configuration versions will appear here after the first change."
        />
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.scroll}>
        <table className={styles.table} aria-label="AI configuration version history">
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Version</th>
              <th scope="col">Description</th>
              <th scope="col">Created</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.versions.map((version) => {
              const isActive = version.version_id === data.active_version_id;
              return (
                <tr
                  key={version.version_id}
                  className={isActive ? styles.activeRow : undefined}
                >
                  <td>
                    {isActive && (
                      <span className={styles.activeBadge} aria-label="Active version">
                        Active
                      </span>
                    )}
                  </td>
                  <td>
                    <span
                      className={styles.versionId}
                      title={version.version_id}
                    >
                      {shortenId(version.version_id, 8, 4)}
                    </span>
                  </td>
                  <td className={styles.description}>
                    {version.change_description}
                  </td>
                  <td title={formatAbsolute(version.created_at)}>
                    {formatRelative(version.created_at)}
                  </td>
                  <td>
                    {!isActive && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => openRollbackDialog(version)}
                        aria-label={`Rollback to version ${shortenId(version.version_id, 8, 4)}`}
                      >
                        <RotateCcw size={14} aria-hidden="true" />
                        Rollback
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Rollback reason capture dialog */}
      <RollbackDialog
        open={rollbackTarget !== null}
        target={rollbackTarget}
        reason={reason}
        onReasonChange={setReason}
        busy={rolling}
        onConfirm={confirmRollback}
        onCancel={closeRollbackDialog}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rollback reason dialog (R9.8 — captures reason for ActivationEvent)
// ---------------------------------------------------------------------------

interface RollbackDialogProps {
  open: boolean;
  target: AIConfigurationVersion | null;
  reason: string;
  onReasonChange: (value: string) => void;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function RollbackDialog({
  open,
  target,
  reason,
  onReasonChange,
  busy,
  onConfirm,
  onCancel,
}: RollbackDialogProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (open) {
      previousFocus.current = document.activeElement as HTMLElement;
      // Delay focus slightly so the dialog renders first
      requestAnimationFrame(() => textareaRef.current?.focus());
      const onKey = (e: KeyboardEvent) => {
        if (e.key === "Escape") onCancel();
      };
      document.addEventListener("keydown", onKey);
      return () => {
        document.removeEventListener("keydown", onKey);
        previousFocus.current?.focus();
      };
    }
  }, [open, onCancel]);

  if (!open || !target) return null;

  const trimmed = reason.trim();
  const isValid = trimmed.length > 0 && trimmed.length <= MAX_REASON_LENGTH;
  const overLimit = reason.length > MAX_REASON_LENGTH;

  return (
    <div className={styles.overlay} onMouseDown={onCancel}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="rollback-title"
        aria-describedby="rollback-body"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 id="rollback-title" className={styles.dialogTitle}>
          Rollback Configuration
        </h2>
        <p id="rollback-body" className={styles.dialogBody}>
          Rolling back to version{" "}
          <strong title={target.version_id}>
            {shortenId(target.version_id, 8, 4)}
          </strong>
          {target.change_description && (
            <> — &ldquo;{target.change_description}&rdquo;</>
          )}
        </p>

        <label htmlFor="rollback-reason">
          <span className={styles.dialogBody}>
            Reason for rollback (required)
          </span>
        </label>
        <textarea
          ref={textareaRef}
          id="rollback-reason"
          className={styles.reasonInput}
          placeholder="Describe why this rollback is needed…"
          value={reason}
          onChange={(e) => onReasonChange(e.target.value)}
          maxLength={MAX_REASON_LENGTH + 50} // Allow slightly over so user sees the count warning
          disabled={busy}
          aria-required="true"
          aria-invalid={overLimit || undefined}
        />
        <span
          className={`${styles.charCount} ${overLimit ? styles.charCountOver : ""}`}
          aria-live="polite"
        >
          {reason.length}/{MAX_REASON_LENGTH}
        </span>

        <div className={styles.dialogActions}>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={onConfirm}
            disabled={!isValid || busy}
          >
            {busy ? "Rolling back…" : "Confirm Rollback"}
          </button>
        </div>
      </div>
    </div>
  );
}
