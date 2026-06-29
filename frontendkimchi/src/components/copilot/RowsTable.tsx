import { useMemo } from "react";
import { deriveColumns, renderCell } from "../../lib/rows";
import styles from "./RowsTable.module.css";

interface RowsTableProps {
  rows: Record<string, unknown>[];
}

export function RowsTable({ rows }: RowsTableProps) {
  const columns = useMemo(() => deriveColumns(rows), [rows]);

  if (rows.length === 0) return null;

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col} scope="col">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((col) => {
                const value = row[col];
                const isNull = value === null || value === undefined;
                return (
                  <td key={col} className={isNull ? styles.null : undefined}>
                    {renderCell(value)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
