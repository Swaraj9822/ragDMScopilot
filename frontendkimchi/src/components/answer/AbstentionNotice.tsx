import { AlertTriangle, ShieldOff } from "lucide-react";
import type { AbstentionResponse, ReasonCode } from "../../api/types";
import styles from "./AbstentionNotice.module.css";

export interface AbstentionNoticeProps {
  /** The abstention response from the backend (R3.9, R3.10). */
  response: AbstentionResponse;
}

/** Default notice shown when `missing_information` is absent or empty (R3.10). */
const DEFAULT_NOTICE =
  "There is not enough evidence in the available sources to answer this question reliably.";

/** Human-friendly labels for each reason code (operator transparency). */
const REASON_LABELS: Record<ReasonCode, string> = {
  low_confidence: "Low confidence",
  no_evidence: "No evidence found",
  unsupported_claims: "Unsupported claims",
  conflicting_evidence: "Conflicting evidence",
  sql_no_rows: "No matching rows",
  retrieval_below_threshold: "Retrieval below threshold",
};

/**
 * Displays an abstention notice when the system lacks sufficient evidence.
 *
 * - Shows `missing_information` description (R3.9)
 * - Surfaces `reason_code` for operator transparency
 * - Never displays an answer
 * - Falls back to a default insufficient-evidence notice when the description
 *   is absent, null, or empty (R3.10)
 *
 * Requirements: 3.9, 3.10
 */
export function AbstentionNotice({ response }: AbstentionNoticeProps) {
  const { reason_code, missing_information } = response;

  const description =
    missing_information && missing_information.trim()
      ? missing_information
      : DEFAULT_NOTICE;

  const reasonLabel =
    REASON_LABELS[reason_code as ReasonCode] ?? reason_code;

  return (
    <article
      className={styles.notice}
      aria-label="The system could not provide an answer"
      role="alert"
    >
      <div className={styles.header}>
        <ShieldOff size={20} className={styles.headerIcon} aria-hidden="true" />
        <span className={styles.title}>Unable to answer</span>
      </div>

      <p className={styles.description}>{description}</p>

      <div className={styles.meta}>
        <AlertTriangle size={14} className={styles.metaIcon} aria-hidden="true" />
        <span className={styles.reasonCode}>
          Reason: <strong>{reasonLabel}</strong>
        </span>
      </div>
    </article>
  );
}
