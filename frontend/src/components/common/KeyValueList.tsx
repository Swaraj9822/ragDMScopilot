import type { AttributeValue } from "../../api/types";
import styles from "./common.module.css";

interface KeyValueListProps {
  /** Attributes; values keep their primitive type (numbers/bools unquoted). */
  data: Record<string, AttributeValue>;
  /** Sort keys alphabetically; default true. */
  sort?: boolean;
}

export function KeyValueList({ data, sort = true }: KeyValueListProps) {
  const entries = Object.entries(data);
  if (sort) entries.sort(([a], [b]) => a.localeCompare(b));

  return (
    <dl className={styles.kvList}>
      {entries.map(([key, value]) => (
        <div key={key} style={{ display: "contents" }}>
          <dt className={styles.kvKey}>{key}</dt>
          <dd className={styles.kvValue} style={{ margin: 0 }}>
            <ValueCell value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ValueCell({ value }: { value: AttributeValue }) {
  if (typeof value === "boolean") {
    return <span className={styles.kvBool}>{String(value)}</span>;
  }
  if (typeof value === "number") {
    return <span className={styles.kvNum}>{String(value)}</span>;
  }
  return <span>{value}</span>;
}
