import styles from "./ViewSwitch.module.css";

export type ObsView = "traces" | "queries" | "logs";

interface ViewSwitchProps {
  value: ObsView;
  onChange: (view: ObsView) => void;
}

const LABELS: Record<ObsView, string> = {
  traces: "Traces",
  queries: "Individual Query",
  logs: "Logs",
};

export function ViewSwitch({ value, onChange }: ViewSwitchProps) {
  return (
    <div className={styles.switch} role="tablist" aria-label="Observability view">
      {(["traces", "queries", "logs"] as const).map((view) => (
        <button
          key={view}
          type="button"
          role="tab"
          aria-selected={value === view}
          className={`${styles.option} ${value === view ? styles.active : ""}`}
          onClick={() => onChange(view)}
        >
          {LABELS[view]}
        </button>
      ))}
    </div>
  );
}
