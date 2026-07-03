import { NavLink } from "react-router-dom";
import {
  Activity,
  Files,
  FlaskConical,
  Gauge,
  Inbox,
  Map,
  MessageSquareText,
} from "lucide-react";
import { useAuth } from "../../hooks/useAuth";
import styles from "./PrimaryNav.module.css";

interface Tab {
  to: string;
  label: string;
  Icon: typeof MessageSquareText;
  operatorOnly?: boolean;
}

const TABS: Tab[] = [
  { to: "/copilot", label: "Copilot", Icon: MessageSquareText },
  { to: "/observability", label: "AI Observability", Icon: Activity },
  { to: "/documents", label: "Documents", Icon: Files },
  { to: "/evaluation", label: "Evaluation", Icon: Gauge, operatorOnly: true },
  { to: "/feedback", label: "Feedback", Icon: Inbox, operatorOnly: true },
  { to: "/replay", label: "Replay Lab", Icon: FlaskConical, operatorOnly: true },
  { to: "/knowledge-gap", label: "Knowledge Gaps", Icon: Map, operatorOnly: true },
];

export function PrimaryNav() {
  const { user } = useAuth();
  const isOperator = user?.is_operator ?? false;

  // NavLink applies aria-current="page" automatically while active.
  return (
    <nav className={styles.nav} aria-label="Primary">
      <div className={styles.inner}>
        {TABS.filter((tab) => !tab.operatorOnly || isOperator).map(
          ({ to, label, Icon }) => (
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
          ),
        )}
      </div>
    </nav>
  );
}
