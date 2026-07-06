/**
 * Pure helper that serialises a SQL Lab Result_Set to a CSV string.
 *
 * When an operator exports a non-empty Result_Set, the SqlLabPage produces a
 * CSV file whose first row is the column names in displayed order and whose
 * subsequent rows are every displayed data row in displayed order, with field
 * values quoted and escaped per standard CSV conventions (Requirement 11.1).
 *
 * This module is intentionally free of React and DOM/Blob concerns so it can be
 * imported directly by a Vitest test (task 15.2 property-tests it) and by the
 * page component (task 15.7 wires it to a download control). The wiring layer
 * is responsible for turning the returned string into a downloadable file.
 */

import type { ResultSet } from "../api/sqlLab";

/**
 * Standard CSV record separator (RFC 4180 uses CRLF). Rows are joined with this
 * terminator; there is no trailing newline after the final row.
 */
const RECORD_SEPARATOR = "\r\n";

/** Field separator between columns within a record. */
const FIELD_SEPARATOR = ",";

/**
 * Characters that force a field to be quoted: a comma, a double-quote, or a
 * carriage return / line feed. Any field containing one of these is wrapped in
 * double quotes with inner double-quotes doubled.
 */
const MUST_QUOTE = /[",\r\n]/;

/**
 * Escape a single already-stringified field per standard CSV conventions.
 *
 * A field is wrapped in double quotes when it contains a comma, a double-quote,
 * a carriage return, or a line feed; any embedded double-quote is doubled.
 * Fields without those characters are emitted verbatim.
 */
function escapeField(value: string): string {
  if (!MUST_QUOTE.test(value)) {
    return value;
  }
  return `"${value.replace(/"/g, '""')}"`;
}

/**
 * Render a single cell to its CSV string form.
 *
 * `null` and `undefined` become empty fields; every other value is rendered via
 * `String(...)` before escaping, so booleans, numbers, and objects serialise
 * with their default string representation.
 */
function cellToString(cell: unknown): string {
  if (cell === null || cell === undefined) {
    return "";
  }
  return String(cell);
}

/**
 * Serialise a Result_Set to a CSV string.
 *
 * The first record is the column names in `result.columns` order; each
 * subsequent record is a row from `result.rows`, in order, with each cell taken
 * from the row keyed by the corresponding column name. Missing keys render as
 * empty fields. Records are separated by CRLF with no trailing terminator, so
 * a Result_Set with N rows yields N + 1 records.
 */
export function toCsv(result: ResultSet): string {
  const { columns, rows } = result;

  const headerRecord = columns.map(escapeField).join(FIELD_SEPARATOR);

  const dataRecords = rows.map((row) =>
    columns
      .map((column) => escapeField(cellToString(row[column])))
      .join(FIELD_SEPARATOR),
  );

  return [headerRecord, ...dataRecords].join(RECORD_SEPARATOR);
}
