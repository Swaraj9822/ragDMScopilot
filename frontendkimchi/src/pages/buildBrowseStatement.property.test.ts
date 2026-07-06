import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  BROWSE_LIMIT,
  buildBrowseStatement,
  quoteIdentifier,
} from "./buildBrowseStatement";

/**
 * Feature: sql-lab, Property 11: Table selection produces the canonical browse statement.
 *
 * For any table name returned by the schema listing, selecting it produces
 * exactly the editor statement `SELECT * FROM "<table>" LIMIT 100` with the
 * selected table's name substituted as a correctly quoted Postgres identifier
 * (and the fixed BROWSE_LIMIT of 100).
 *
 * Validates: Requirements 7.8
 */

describe("buildBrowseStatement property 11", () => {
  it("produces the canonical browse statement with a quoted identifier for any table name", () => {
    fc.assert(
      fc.property(fc.string(), (tableName) => {
        expect(buildBrowseStatement(tableName)).toBe(
          `SELECT * FROM ${quoteIdentifier(tableName)} LIMIT 100`,
        );
      }),
      { numRuns: 500 },
    );
  });

  it("quotes the identifier (doubling embedded quotes) and uses the fixed BROWSE_LIMIT", () => {
    // Guard against any regression that changes the fixed limit.
    expect(BROWSE_LIMIT).toBe(100);

    fc.assert(
      fc.property(fc.string(), (tableName) => {
        const stmt = buildBrowseStatement(tableName);

        // Structure: SELECT * FROM "<escaped>" LIMIT 100
        const prefix = 'SELECT * FROM "';
        const suffix = `" LIMIT ${BROWSE_LIMIT}`;
        expect(stmt.startsWith(prefix)).toBe(true);
        expect(stmt.endsWith(suffix)).toBe(true);

        // The body between the surrounding quotes, with doubled quotes
        // collapsed, round-trips back to the original name — i.e. every
        // embedded double quote was escaped by doubling.
        const body = stmt.slice(prefix.length, stmt.length - suffix.length);
        expect(body.replace(/""/g, '"')).toBe(tableName);
      }),
      { numRuns: 500 },
    );
  });
});
