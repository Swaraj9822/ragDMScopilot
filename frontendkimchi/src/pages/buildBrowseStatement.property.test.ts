import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { BROWSE_LIMIT, buildBrowseStatement } from "./buildBrowseStatement";

/**
 * Feature: sql-lab, Property 11: Table selection produces the canonical browse statement.
 *
 * For any table name returned by the schema listing, selecting it produces
 * exactly the editor statement `SELECT * FROM <table> LIMIT 100` with the
 * selected table's name substituted (and the fixed BROWSE_LIMIT of 100).
 *
 * Validates: Requirements 7.8
 */

describe("buildBrowseStatement property 11", () => {
  it("produces the canonical browse statement for any table name", () => {
    fc.assert(
      fc.property(fc.string(), (tableName) => {
        expect(buildBrowseStatement(tableName)).toBe(
          `SELECT * FROM ${tableName} LIMIT 100`,
        );
      }),
      { numRuns: 500 },
    );
  });

  it("substitutes the table name verbatim and uses the fixed BROWSE_LIMIT", () => {
    // Guard against any regression that changes the fixed limit.
    expect(BROWSE_LIMIT).toBe(100);

    fc.assert(
      fc.property(fc.string(), (tableName) => {
        expect(buildBrowseStatement(tableName)).toBe(
          `SELECT * FROM ${tableName} LIMIT ${BROWSE_LIMIT}`,
        );
      }),
      { numRuns: 500 },
    );
  });
});
