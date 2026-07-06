import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  computeChartSpecData,
  type AllowedOp,
  type ChartDef,
  type ChartSpec,
  type ChartType,
  type KpiSpec,
} from "./computeChartSpecData";
import type { ResultSet } from "../api/sqlLab";

/**
 * Feature: sql-lab, Property 15: The dashboard computes every displayed value
 * locally from the rows.
 *
 * For any validated ChartSpec and ResultSet, `computeChartSpecData` computes
 * each KPI/series value locally from the actual rows using only the declared op
 * over the referenced column; any KPI/chart referencing an unknown column or a
 * disallowed op is omitted/marked uncomputable (never fabricated).
 *
 * The test proves this two ways:
 *  1. For specs referencing only real columns with allowed ops, every computed
 *     value EQUALS an aggregate re-computed independently from the actual rows
 *     (sum/count/avg/min/max recomputed here in the test).
 *  2. For specs referencing unknown columns or disallowed ops, the affected
 *     KPI/chart is marked `computable: false` with a `null` value (KPIs) or an
 *     empty series/data (charts) instead of throwing or inventing a number.
 *
 * Validates: Requirements 10.3, 10.4
 */

const ALLOWED_OPS: readonly AllowedOp[] = ["sum", "count", "avg", "min", "max"];
const CHART_TYPES: readonly ChartType[] = ["bar", "line", "pie"];
const COLUMN_POOL = ["a", "b", "c", "d", "e"] as const;
/** Names deliberately disjoint from COLUMN_POOL, so they are always unknown. */
const UNKNOWN_COLUMNS = ["__nope__", "__missing__", "__ghost__"] as const;
/** Operations outside the bounded allowed set. */
const DISALLOWED_OPS = ["median", "stddev", "mode", "first", "", "COUNT"] as const;

// ---------------------------------------------------------------------------
// Independent re-implementations of the aggregation semantics.
// These mirror the documented SQL-like semantics of the implementation but are
// written independently so the property genuinely re-derives each value.
// ---------------------------------------------------------------------------

/** Coerce a cell to a finite number, else null (numbers + numeric strings). */
function coerce(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.length === 0) {
      return null;
    }
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** Stable group label, mirroring the implementation's `toLabel`. */
function toLabel(value: unknown): string {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  return String(value);
}

/** Independently compute the expected aggregate over the given rows. */
function expectedAggregate(
  rows: ReadonlyArray<Record<string, unknown>>,
  column: string,
  op: AllowedOp,
): number | null {
  if (op === "count") {
    let count = 0;
    for (const row of rows) {
      if (row[column] != null) {
        count += 1;
      }
    }
    return count;
  }

  const numbers: number[] = [];
  for (const row of rows) {
    const n = coerce(row[column]);
    if (n !== null) {
      numbers.push(n);
    }
  }

  if (op === "sum") {
    let total = 0;
    for (const n of numbers) {
      total += n;
    }
    return total;
  }
  if (numbers.length === 0) {
    return null;
  }
  switch (op) {
    case "avg": {
      let total = 0;
      for (const n of numbers) {
        total += n;
      }
      return total / numbers.length;
    }
    case "min":
      return Math.min(...numbers);
    case "max":
      return Math.max(...numbers);
  }
}

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/** A single cell: a mix of numeric and non-numeric values. */
const cellArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.integer({ min: -1000, max: 1000 }),
  fc.integer({ min: -1000, max: 1000 }).map((n) => String(n)), // numeric string
  fc.string(), // usually non-numeric text
  fc.boolean(),
  fc.constant(null),
);

/** A distinct, non-empty subset of the column pool. */
const columnsArb: fc.Arbitrary<string[]> = fc.uniqueArray(
  fc.constantFrom(...COLUMN_POOL),
  { minLength: 1, maxLength: COLUMN_POOL.length },
);

/** Build a row containing every column (each mapped to a random cell). */
function rowArb(columns: string[]): fc.Arbitrary<Record<string, unknown>> {
  return fc.tuple(...columns.map(() => cellArb)).map((values) => {
    const row: Record<string, unknown> = {};
    columns.forEach((c, i) => {
      row[c] = values[i];
    });
    return row;
  });
}

function makeResultSet(
  columns: string[],
  rows: Record<string, unknown>[],
): ResultSet {
  return {
    columns,
    rows,
    rowCount: rows.length,
    durationMs: 0,
    sql: "",
    truncated: false,
  };
}

const opArb: fc.Arbitrary<AllowedOp> = fc.constantFrom(...ALLOWED_OPS);
const chartTypeArb: fc.Arbitrary<ChartType> = fc.constantFrom(...CHART_TYPES);

/** A ResultSet plus a ChartSpec that references only real columns + allowed ops. */
const validCaseArb = columnsArb.chain((columns) => {
  const colArb = fc.constantFrom(...columns);
  const kpiArb: fc.Arbitrary<KpiSpec> = fc.record({
    label: fc.string(),
    op: opArb,
    column: colArb,
  });
  const chartArb: fc.Arbitrary<ChartDef> = fc.record({
    type: chartTypeArb,
    title: fc.string(),
    xColumn: colArb,
    series: fc.array(fc.record({ column: colArb, op: opArb }), {
      minLength: 1,
      maxLength: 3,
    }),
  });
  const specArb: fc.Arbitrary<ChartSpec> = fc.record({
    kpis: fc.array(kpiArb, { maxLength: 4 }),
    charts: fc.array(chartArb, { maxLength: 3 }),
    insight: fc.option(fc.string(), { nil: null }),
  });
  return fc
    .array(rowArb(columns), { maxLength: 20 })
    .chain((rows) =>
      specArb.map((spec) => ({ resultSet: makeResultSet(columns, rows), spec })),
    );
});

/** A ResultSet plus a ChartSpec whose every KPI/chart is guaranteed invalid. */
const invalidCaseArb = columnsArb.chain((columns) => {
  const colArb = fc.constantFrom(...columns);
  const badColArb = fc.constantFrom(...UNKNOWN_COLUMNS);
  const badOpArb = fc.constantFrom(...DISALLOWED_OPS);

  // Each invalid KPI is either (unknown column + allowed op) or
  // (real column + disallowed op) — both must be uncomputable.
  const invalidKpiArb: fc.Arbitrary<KpiSpec> = fc.oneof(
    fc.record({ label: fc.string(), op: opArb, column: badColArb }),
    fc.record({
      label: fc.string(),
      op: badOpArb as unknown as fc.Arbitrary<AllowedOp>,
      column: colArb,
    }),
  );

  // Each invalid chart is either (unknown xColumn), (real xColumn + a series
  // with a disallowed op), or (real xColumn + a series with an unknown column).
  const invalidChartArb: fc.Arbitrary<ChartDef> = fc.oneof(
    fc.record({
      type: chartTypeArb,
      title: fc.string(),
      xColumn: badColArb,
      series: fc.array(fc.record({ column: colArb, op: opArb }), {
        minLength: 1,
        maxLength: 3,
      }),
    }),
    fc.record({
      type: chartTypeArb,
      title: fc.string(),
      xColumn: colArb,
      series: fc.array(
        fc.record({
          column: colArb,
          op: badOpArb as unknown as fc.Arbitrary<AllowedOp>,
        }),
        { minLength: 1, maxLength: 3 },
      ),
    }),
    fc.record({
      type: chartTypeArb,
      title: fc.string(),
      xColumn: colArb,
      series: fc.array(fc.record({ column: badColArb, op: opArb }), {
        minLength: 1,
        maxLength: 3,
      }),
    }),
  );

  const specArb: fc.Arbitrary<ChartSpec> = fc.record({
    kpis: fc.array(invalidKpiArb, { minLength: 1, maxLength: 4 }),
    charts: fc.array(invalidChartArb, { minLength: 1, maxLength: 3 }),
    insight: fc.option(fc.string(), { nil: null }),
  });

  return fc
    .array(rowArb(columns), { maxLength: 20 })
    .chain((rows) =>
      specArb.map((spec) => ({ resultSet: makeResultSet(columns, rows), spec })),
    );
});

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------

describe("computeChartSpecData property 15", () => {
  it("computes every KPI value locally from the rows (== independent aggregate)", () => {
    fc.assert(
      fc.property(validCaseArb, ({ resultSet, spec }) => {
        const computed = computeChartSpecData(spec, resultSet);

        expect(computed.kpis).toHaveLength(spec.kpis.length);
        computed.kpis.forEach((kpi, i) => {
          const declared = spec.kpis[i];
          // Referencing a real column with an allowed op => computable.
          expect(kpi.computable).toBe(true);
          expect(kpi.reason).toBeUndefined();
          // The value equals the aggregate re-computed independently here.
          expect(kpi.value).toEqual(
            expectedAggregate(resultSet.rows, declared.column, declared.op),
          );
        });
      }),
      { numRuns: 300 },
    );
  });

  it("computes every chart series value locally per group from the rows", () => {
    fc.assert(
      fc.property(validCaseArb, ({ resultSet, spec }) => {
        const computed = computeChartSpecData(spec, resultSet);

        expect(computed.charts).toHaveLength(spec.charts.length);
        computed.charts.forEach((chart, i) => {
          const declared = spec.charts[i];
          expect(chart.computable).toBe(true);
          expect(chart.reason).toBeUndefined();
          expect(chart.series).toHaveLength(declared.series.length);

          for (const point of chart.data) {
            // Re-derive the group's rows using the same stable group key.
            const groupKey = `${typeof point.x}:${point.xLabel}`;
            const groupRows = resultSet.rows.filter(
              (row) =>
                `${typeof row[declared.xColumn]}:${toLabel(row[declared.xColumn])}` ===
                groupKey,
            );
            chart.series.forEach((series, sIndex) => {
              const declaredSeries = declared.series[sIndex];
              expect(point[series.key]).toEqual(
                expectedAggregate(
                  groupRows,
                  declaredSeries.column,
                  declaredSeries.op,
                ),
              );
            });
          }
        });
      }),
      { numRuns: 300 },
    );
  });

  it("never fabricates: unknown column / disallowed op is marked uncomputable", () => {
    fc.assert(
      fc.property(invalidCaseArb, ({ resultSet, spec }) => {
        // Must not throw on invalid references.
        const computed = computeChartSpecData(spec, resultSet);

        // Every KPI is uncomputable with a null value and a stated reason.
        expect(computed.kpis).toHaveLength(spec.kpis.length);
        for (const kpi of computed.kpis) {
          expect(kpi.computable).toBe(false);
          expect(kpi.value).toBeNull();
          expect(typeof kpi.reason).toBe("string");
          expect(kpi.reason!.length).toBeGreaterThan(0);
        }

        // Every chart is uncomputable with no series and no fabricated data.
        expect(computed.charts).toHaveLength(spec.charts.length);
        for (const chart of computed.charts) {
          expect(chart.computable).toBe(false);
          expect(chart.series).toEqual([]);
          expect(chart.data).toEqual([]);
          expect(typeof chart.reason).toBe("string");
          expect(chart.reason!.length).toBeGreaterThan(0);
        }
      }),
      { numRuns: 300 },
    );
  });
});
