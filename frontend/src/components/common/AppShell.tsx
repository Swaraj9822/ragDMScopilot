import type { ReactNode } from "react";
import { PrimaryNav } from "./PrimaryNav";
import { ConnectionStatus } from "./ConnectionStatus";
import { ThemeToggle } from "./ThemeToggle";
import { ToastRegion } from "./ToastRegion";
import styles from "./AppShell.module.css";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className={styles.brandText}>
            <span className={styles.name}>RAG Console</span>
            <span className={styles.descriptor}>Copilot · Telemetry · Knowledge</span>
          </span>
        </div>
        <div className={styles.topbarRight}>
          <ConnectionStatus />
          <ThemeToggle />
        </div>
      </header>
      <PrimaryNav />
      <main id="main-content" className={styles.main}>
        {children}
      </main>
      <ToastRegion />
    </div>
  );
}
