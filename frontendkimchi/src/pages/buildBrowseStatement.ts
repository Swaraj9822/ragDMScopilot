/**
 * Pure helper that builds the canonical "quick browse" statement for a table
 * selected from the schema sidebar.
 *
 * When an operator selects a table in the sidebar, the SqlLabPage replaces the
 * entire editor contents with a statement of the form
 * `SELECT * FROM <table> LIMIT 100`, where `<table>` is the selected table's
 * name (Requirement 7.8).
 *
 * This module is intentionally free of React so it can be imported directly by
 * a Vitest test (task 10.6 property-tests it) and by the page component.
 */

/** The row limit applied to the generated quick-browse statement. */
export const BROWSE_LIMIT = 100;

/**
 * Quote a value as a Postgres identifier: wrap it in double quotes and escape
 * any embedded double quote by doubling it (the standard SQL identifier-quoting
 * rule).
 *
 * Quoting makes the generated statement valid and exact for table names that
 * contain uppercase letters, spaces, reserved words, or punctuation — an
 * unquoted `SELECT * FROM My Table` or `SELECT * FROM order` would otherwise be
 * invalid SQL. Because a quoted identifier is matched verbatim (Postgres does
 * not case-fold it), quoting the exact name returned by the schema listing is
 * the correct behavior.
 */
export function quoteIdentifier(identifier: string): string {
  return `"${identifier.replace(/"/g, '""')}"`;
}

/**
 * Produce the canonical browse statement for the given table name:
 * `SELECT * FROM "<tableName>" LIMIT 100`.
 *
 * The table name is quoted as a Postgres identifier (see {@link quoteIdentifier})
 * so the statement is valid regardless of the name's casing or characters, and
 * is deterministic for any input string.
 */
export function buildBrowseStatement(tableName: string): string {
  return `SELECT * FROM ${quoteIdentifier(tableName)} LIMIT ${BROWSE_LIMIT}`;
}
