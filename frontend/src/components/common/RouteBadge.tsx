import { routeLabel } from "../../lib/status";
import styles from "./common.module.css";

export function RouteBadge({ route }: { route: string }) {
  return <span className={styles.routeBadge}>{routeLabel(route)}</span>;
}
