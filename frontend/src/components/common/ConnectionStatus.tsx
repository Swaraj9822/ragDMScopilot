import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useHealth } from "../../hooks/useHealth";
import { API_BASE_URL } from "../../api/client";
import styles from "./ConnectionStatus.module.css";

export function ConnectionStatus() {
  const { state, refetch, isFetching } = useHealth();
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const connected = state === "connected";
  const label = connected
    ? "API connected"
    : state === "checking"
      ? "Checking API…"
      : "API unavailable";

  const dotClass =
    state === "connected"
      ? styles.dotOk
      : state === "checking"
        ? styles.dotChecking
        : styles.dotFail;

  return (
    <div className={styles.wrap} ref={popoverRef}>
      <button
        type="button"
        className={styles.trigger}
        onClick={() => !connected && setOpen((v) => !v)}
        aria-expanded={connected ? undefined : open}
        aria-label={label}
        data-static={connected}
      >
        <span className={`${styles.dot} ${dotClass}`} aria-hidden="true" />
        <span className={styles.label}>{label}</span>
      </button>
      {open && !connected && (
        <div className={styles.popover} role="dialog" aria-label="API connection details">
          <p className={styles.popTitle}>Backend unavailable</p>
          <p className={styles.popBody}>
            Could not reach the API at:
          </p>
          <code className={styles.url}>{API_BASE_URL}</code>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw size={14} aria-hidden="true" />
            {isFetching ? "Retrying…" : "Retry"}
          </button>
        </div>
      )}
    </div>
  );
}
