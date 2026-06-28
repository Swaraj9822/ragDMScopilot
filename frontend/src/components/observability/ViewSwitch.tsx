import styles from "./ViewSwitch.module.css";

export type ObsView = "traces" | "logs";

interface ViewSwitchProps {
  value: ObsView;
  onChange: (view: ObsView) => void;
}

export function ViewSwitch({ value, onChange }: ViewSwitchProps) {
  return (
    <div className={styles.switch} role="tablist" aria-label="Observability view">
      {(["traces", "logs"] as const).map((view) => (
        <button
          key={view}
          type="button"
          role="tab"
          aria-selected={value === view}
          className={`${styles.option} ${value === view ? styles.active : ""}`}
          onClick={() => onChange(view)}
        >
          {view === "traces" ? "Traces" : "Logs"}
        </button>
      ))}
    </div>
  );
}
