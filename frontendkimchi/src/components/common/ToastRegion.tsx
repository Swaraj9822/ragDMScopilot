import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import type { ToastTone } from "../../hooks/useToast";
import { useToast } from "../../hooks/useToast";
import styles from "./ToastRegion.module.css";

const TONE_ICON = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: XCircle,
} as const;

export function ToastRegion() {
  const { toasts, dismissToast } = useToast();
  return (
    <div className={styles.region} role="status" aria-live="polite" aria-atomic="false">
      {toasts.map((toast) => {
        const Icon = TONE_ICON[toast.tone as ToastTone] ?? Info;
        return (
          <div key={toast.id} className={`${styles.toast} ${styles[toast.tone]}`}>
            <Icon size={16} className={styles.icon} aria-hidden="true" />
            <span className={styles.message}>{toast.message}</span>
            <button
              type="button"
              className={styles.close}
              onClick={() => dismissToast(toast.id)}
              aria-label="Dismiss notification"
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
