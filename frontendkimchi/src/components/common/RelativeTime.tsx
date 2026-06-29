import { useEffect, useState } from "react";
import { formatAbsolute, formatRelative } from "../../lib/format";
import styles from "./common.module.css";

interface RelativeTimeProps {
  iso: string;
  /** Refresh interval in ms; default 30s. */
  interval?: number;
}

export function RelativeTime({ iso, interval = 30_000 }: RelativeTimeProps) {
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), interval);
    return () => clearInterval(id);
  }, [interval]);

  const absolute = formatAbsolute(iso);
  return (
    <time className={styles.relTime} dateTime={iso} title={absolute}>
      {formatRelative(iso)}
    </time>
  );
}
