import { statusTone, TONE_COLOR_VAR, TONE_SOFT_VAR, type StatusTone } from "../../lib/status";
import styles from "./common.module.css";

interface StatusBadgeProps {
  /** Raw status value (e.g. "indexed", "error", "WARNING"). */
  status: string;
  /** Optional override label; defaults to the status value. */
  label?: string;
  tone?: StatusTone;
}

export function StatusBadge({ status, label, tone }: StatusBadgeProps) {
  const resolved = tone ?? statusTone(status);
  const color = `var(${TONE_COLOR_VAR[resolved]})`;
  const soft = `var(${TONE_SOFT_VAR[resolved]})`;
  return (
    <span
      className={styles.badge}
      style={{ background: soft, color, borderColor: color }}
    >
      <span className={styles.badgeDot} style={{ background: color }} aria-hidden="true" />
      {label ?? status}
    </span>
  );
}
