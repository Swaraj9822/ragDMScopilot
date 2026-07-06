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
 * Produce the canonical browse statement for the given table name:
 * `SELECT * FROM <tableName> LIMIT 100`.
 *
 * The table name is echoed verbatim (the schema-listing endpoint only returns
 * names the viewer role may read), so the result is deterministic for any
 * input string.
 */
export function buildBrowseStatement(tableName: string): string {
  return `SELECT * FROM ${tableName} LIMIT ${BROWSE_LIMIT}`;
}
