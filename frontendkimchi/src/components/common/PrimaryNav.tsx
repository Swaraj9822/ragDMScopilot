import { NavLink } from "react-router-dom";
import { Activity, Files, MessageSquareText } from "lucide-react";
import styles from "./PrimaryNav.module.css";

const TABS = [
  { to: "/copilot", label: "Copilot", Icon: MessageSquareText },
  { to: "/observability", label: "AI Observability", Icon: Activity },
  { to: "/documents", label: "Documents", Icon: Files },
] as const;

export function PrimaryNav() {
  // NavLink applies aria-current="page" automatically while active.
  return (
    <nav className={styles.nav} aria-label="Primary">
      <div className={styles.inner}>
        {TABS.map(({ to, label, Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              isActive ? `${styles.tab} ${styles.active}` : styles.tab
            }
          >
            <Icon size={16} aria-hidden="true" />
            <span>{label}</span>
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
