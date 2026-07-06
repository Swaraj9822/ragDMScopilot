import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { toCsv } from "./toCsv";
import type { ResultSet } from "../api/sqlLab";

/**
 * Feature: sql-lab, Property 17: CSV export round-trips columns and rows in
 * displayed order.
 *
 * For any Result_Set, `toCsv` emits the column names first (in order) then every
 * displayed row in order, with standard CSV quoting/escaping, such that parsing
 * the CSV back yields the original columns and cell values (as strings) in the
 * same order. `null`/`undefined` cells render as empty strings.
 *
 * Validates: Requirements 11.1
 */

/**
 * A minimal, correct RFC 4180 CSV parser used to round-trip `toCsv` output.
 *
 * Records are separated by CRLF; fields by commas. A field may be quoted with
 * double quotes, in which case embedded commas, CRLFs, and doubled quotes are
 * literal. This is intentionally independent of `toCsv` so the test verifies
 * real serialise/parse behaviour rather than a shared implementation.
 */
function parseCsv(text: string): string[][] {
  const records: string[][] = [];
  let record: string[] = [];
  let field = "";
  let inQuotes = false;
  let i = 0;

  while (i < text.length) {
    const ch = text[i];

    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i += 1;
        continue;
      }
      field += ch;
      i += 1;
      continue;
    }

    if (ch === '"') {
      inQuotes = true;
      i += 1;
      continue;
    }
    if (ch === ",") {
      record.push(field);
      field = "";
      i += 1;
      continue;
    }
    if (ch === "\r" && text[i + 1] === "\n") {
      record.push(field);
      records.push(record);
      record = [];
      field = "";
      i += 2;
      continue;
    }
    field += ch;
    i += 1;
  }

  // Flush the final field/record (there is no trailing separator).
  record.push(field);
  records.push(record);
  return records;
}

/**
 * Strings that stress CSV escaping: commas, double quotes, carriage returns,
 * line feeds, and combinations, alongside ordinary text.
 */
const nastyStringArb: fc.Arbitrary<string> = fc.oneof(
  fc.string(),
  fc.constantFrom(
    "",
    ",",
    '"',
    '""',
    "a,b",
    'he said "hi"',
    "line1\r\nline2",
    "trailing\n",
    "\rleading",
    "a\tb",
    "  spaces  ",
    "mix,\"quote\"\r\nnewline",
  ),
  // Build arbitrary strings drawn from a special-character alphabet so quoting
  // and escaping are exercised densely.
  fc
    .array(fc.constantFrom("a", "b", ",", '"', "\r", "\n", " "), { maxLength: 8 })
    .map((chars) => chars.join("")),
);

/** An arbitrary cell value: nasty strings, numbers, booleans, and null. */
const cellArb: fc.Arbitrary<unknown> = fc.oneof(
  nastyStringArb,
  fc.integer(),
  fc.double({ noNaN: true }),
  fc.boolean(),
  fc.constant(null),
);

/**
 * An arbitrary Result_Set with unique column names and rows keyed by those
 * columns. Values include strings with commas/quotes/newlines, numbers, and
 * null.
 */
const resultSetArb: fc.Arbitrary<ResultSet> = fc
  .uniqueArray(nastyStringArb, { minLength: 1, maxLength: 6 })
  .chain((columns) =>
    fc
      .array(
        fc.tuple(...columns.map(() => cellArb)),
        { maxLength: 12 },
      )
      .map((rowTuples) => {
        const rows = rowTuples.map((tuple) => {
          const row: Record<string, unknown> = {};
          columns.forEach((column, index) => {
            row[column] = tuple[index];
          });
          return row;
        });
        return {
          columns,
          rows,
          rowCount: rows.length,
          durationMs: 0,
          sql: "SELECT 1",
          truncated: false,
        } satisfies ResultSet;
      }),
  );

/** The expected string form of a cell: null/undefined → "", else String(cell). */
function expectedCell(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

describe("toCsv property 17", () => {
  it("emits the escaped column names, in order, as the first record", () => {
    fc.assert(
      fc.property(resultSetArb, (result) => {
        const csv = toCsv(result);
        const [headerRecord] = parseCsv(csv);
        expect(headerRecord).toEqual(result.columns);
      }),
      { numRuns: 300 },
    );
  });

  it("round-trips columns and rows in displayed order", () => {
    fc.assert(
      fc.property(resultSetArb, (result) => {
        const csv = toCsv(result);
        const records = parseCsv(csv);

        // One header record plus one record per displayed row, in order.
        expect(records.length).toBe(result.rows.length + 1);
        expect(records[0]).toEqual(result.columns);

        result.rows.forEach((row, rowIndex) => {
          const expectedRecord = result.columns.map((column) =>
            expectedCell(row[column]),
          );
          expect(records[rowIndex + 1]).toEqual(expectedRecord);
        });
      }),
      { numRuns: 300 },
    );
  });
});
