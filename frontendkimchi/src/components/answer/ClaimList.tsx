import {
  CheckCircle,
  CircleDot,
  XCircle,
  HelpCircle,
} from "lucide-react";
import type { Claim, EvidenceStatus } from "../../api/types";
import styles from "./ClaimList.module.css";

interface ClaimListProps {
  claims: Claim[];
  /** Optional callback when a claim is selected (e.g. to open the evidence panel). */
  onSelectClaim?: (claim: Claim) => void;
}

/**
 * Renders a list of claims with accessible, non-color-only status indicators.
 *
 * Each status uses a unique combination of:
 * - Icon (distinct shape per status)
 * - Text label (explicit readable status)
 * - Visual shape / border style (pill, notched, square, dashed)
 *
 * This satisfies R1.10 (distinct per status) and R1.13 (does not rely on color alone).
 */
export function ClaimList({ claims, onSelectClaim }: ClaimListProps) {
  if (claims.length === 0) {
    return null;
  }

  return (
    <ul className={styles.list} aria-label="Answer claims">
      {claims.map((claim) => (
        <li key={claim.claim_id} className={styles.claim}>
          <StatusIndicator status={claim.evidence_status as EvidenceStatus} />
          <span className={styles.claimText}>
            {onSelectClaim ? (
              <button
                type="button"
                onClick={() => onSelectClaim(claim)}
                style={{
                  all: "unset",
                  cursor: "pointer",
                  textDecoration: "underline",
                  textDecorationStyle: "dotted",
                  textUnderlineOffset: "2px",
                }}
                aria-label={`View evidence for claim: ${claim.text}`}
              >
                {claim.text}
              </button>
            ) : (
              claim.text
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Status indicator component
// ---------------------------------------------------------------------------

/** Status label text shown alongside the icon. */
const STATUS_LABELS: Record<EvidenceStatus, string> = {
  supported: "Supported",
  partially_supported: "Partial",
  unsupported: "Unsupported",
  verification_unavailable: "Unverified",
};

interface StatusIndicatorProps {
  status: EvidenceStatus | string;
}

/**
 * Renders an accessible status indicator with a unique icon, text label,
 * and container shape for each of the four evidence statuses.
 *
 * Accessibility: not color-only — each status has a distinct icon shape
 * (CheckCircle, CircleDot, XCircle, HelpCircle) and an explicit text label.
 */
export function StatusIndicator({ status }: StatusIndicatorProps) {
  const label = STATUS_LABELS[status as EvidenceStatus] ?? status;

  switch (status) {
    case "supported":
      return (
        <span
          className={`${styles.indicator} ${styles.indicatorSupported}`}
          aria-label={`Evidence status: ${label}`}
          role="status"
        >
          <CheckCircle size={14} className={styles.indicatorIcon} aria-hidden="true" />
          {label}
        </span>
      );
    case "partially_supported":
      return (
        <span
          className={`${styles.indicator} ${styles.indicatorPartial}`}
          aria-label={`Evidence status: ${label}`}
          role="status"
        >
          <CircleDot size={14} className={styles.indicatorIcon} aria-hidden="true" />
          {label}
        </span>
      );
    case "unsupported":
      return (
        <span
          className={`${styles.indicator} ${styles.indicatorUnsupported}`}
          aria-label={`Evidence status: ${label}`}
          role="status"
        >
          <XCircle size={14} className={styles.indicatorIcon} aria-hidden="true" />
          {label}
        </span>
      );
    case "verification_unavailable":
      return (
        <span
          className={`${styles.indicator} ${styles.indicatorUnavailable}`}
          aria-label={`Evidence status: ${label}`}
          role="status"
        >
          <HelpCircle size={14} className={styles.indicatorIcon} aria-hidden="true" />
          {label}
        </span>
      );
    default:
      // Defensive: unknown status gets the "unavailable" treatment.
      return (
        <span
          className={`${styles.indicator} ${styles.indicatorUnavailable}`}
          aria-label={`Evidence status: ${status}`}
          role="status"
        >
          <HelpCircle size={14} className={styles.indicatorIcon} aria-hidden="true" />
          {status}
        </span>
      );
  }
}
