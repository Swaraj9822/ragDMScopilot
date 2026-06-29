import type { LucideIcon } from "lucide-react";
import { Inbox } from "lucide-react";
import type { ReactNode } from "react";
import styles from "./common.module.css";

interface EmptyStateProps {
  title: string;
  body?: string;
  icon?: LucideIcon;
  action?: ReactNode;
}

export function EmptyState({ title, body, icon: Icon = Inbox, action }: EmptyStateProps) {
  return (
    <div className={`${styles.emptyState} enter`}>
      <Icon size={32} className={styles.emptyIcon} aria-hidden="true" />
      <p className={styles.stateTitle}>{title}</p>
      {body && <p className={styles.stateBody}>{body}</p>}
      {action}
    </div>
  );
}
