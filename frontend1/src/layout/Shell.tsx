import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { MessageSquare, Activity, FileUp, Wifi, WifiOff } from "lucide-react";
import { useHealth } from "../hooks/useHealth";
import styles from "./Shell.module.css";

const NAV = [
  { to: "/copilot", label: "Copilot", icon: MessageSquare },
  { to: "/observability", label: "Observability", icon: Activity },
  { to: "/documents", label: "Documents", icon: FileUp },
] as const;

export function Shell({ children }: { children: ReactNode }) {
  const health = useHealth();

  return (
    <div className={styles.shell}>
      <a className="skip-link" href="#main">Skip to content</a>

      <aside className={styles.sidebar}>
        <div className={styles.logo}>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <rect width="24" height="24" rx="6" fill="var(--primary)" />
            <path d="M7 8h10M7 12h7M7 16h9" stroke="white" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
          <span className={styles.logoText}>RAG Console</span>
        </div>

        <nav className={styles.nav} aria-label="Main navigation">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `${styles.navItem} ${isActive ? styles.navActive : ""}`
              }
            >
              <Icon size={18} aria-hidden="true" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className={styles.sidebarFooter}>
          <div className={styles.healthDot}>
            {health === "connected" ? (
              <><Wifi size={14} /><span className={styles.healthLabel}>Connected</span></>
            ) : health === "unavailable" ? (
              <><WifiOff size={14} style={{ color: "var(--danger)" }} /><span className={styles.healthLabel} style={{ color: "var(--danger)" }}>Offline</span></>
            ) : (
              <><Wifi size={14} style={{ opacity: 0.4 }} /><span className={styles.healthLabel}>Checking…</span></>
            )}
          </div>
        </div>
      </aside>

      <main id="main" className={styles.main}>
        {children}
      </main>
    </div>
  );
}
