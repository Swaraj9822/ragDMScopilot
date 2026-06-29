import { AlertTriangle } from "lucide-react";
import type { ReactNode } from "react";
import styles from "./common.module.css";

interface ErrorStateProps {
  title?: string;
  body?: string;
  action?: ReactNode;
}

export function ErrorState({
  title = "Something went wrong",
  body,
  action,
}: ErrorStateProps) {
  return (
    <div className={styles.errorState} role="alert">
      <AlertTriangle size={32} className={styles.errorIcon} aria-hidden="true" />
      <p className={styles.stateTitle}>{title}</p>
      {body && <p className={styles.stateBody}>{body}</p>}
      {action}
    </div>
  );
}
