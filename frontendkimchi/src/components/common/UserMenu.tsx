import { LogOut } from "lucide-react";
import { useAuth } from "../../hooks/useAuth";
import styles from "./UserMenu.module.css";

export function UserMenu() {
  const { user, status, logout } = useAuth();
  if (status !== "authenticated" || !user) return null;
  return (
    <div className={styles.wrap}>
      <span className={styles.email} title={user.email}>
        {user.email}
      </span>
      <button
        type="button"
        className="btn btn-icon"
        onClick={() => {
          void logout();
        }}
        aria-label="Sign out"
        title="Sign out"
      >
        <LogOut size={16} aria-hidden="true" />
      </button>
    </div>
  );
}
