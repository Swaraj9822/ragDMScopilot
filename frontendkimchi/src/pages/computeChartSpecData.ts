/**
 * Pure helper that turns a validated, declarative `ChartSpec` plus its source
 * `ResultSet` into concrete, locally computed dashboard numbers.
 *
 * The AI auto-dashboard (Slice 4) never trusts the language model to emit
 * numbers: the `ChartSpec` carries only column names and operations drawn from a
 * bounded allowed set (`sum`, `count`, `avg`, `min`, `max`, plus group-by via a
 * chart's `xColumn`). This helper computes every displayed value locally from
 * the actual returned rows (Requirement 10.3), so the dashboard can never show a
 * value the data does not support.
 *
 * Any KPI or chart that references a column absent from `ResultSet.columns`, or
 * an operation outside the allowed set, is **not thrown away by an exception**:
 * it is marked `uncomputable` with a human-readable reason so the UI can omit it
 * and indicate that it could not be computed (Requirement 10.4).
 *
 * The module is intentionally free of React so it can be imported directly by a
 * Vitest test (task 13.2 property-tests it) and by the `AutoDashboard`
 * component (wired in task 13.5).
 *
 * The `ChartSpec` types below mirror the backend schema in
 * `rag_system/sql_lab/chart_spec.py` so both layers agree on the contract.
 */

import type { ResultSet } from "../api/sqlLab";
import { boundedInsight } from "./boundedInsight";

// ---------------------------------------------------------------------------
// ChartSpec types (mirror of the backend `chart_spec.py` declarative schema)
// ---------------------------------------------------------------------------

/** The bounded set of allowed aggregation operations (mirrors backend `AllowedOp`). */
export type AllowedOp = "sum" | "count" | "avg" | "min" | "max";

/** The bounded set of allowed chart types (mirrors backend `ChartType`). */
export type ChartType = "bar" | "line" | "pie";

/** All allowed operations as a runtime set, used to reject disallowed ops. */
export const ALLOWED_OPS: readonly AllowedOp[] = [
  "sum",
  "count",
  "avg",
  "min",
  "max",
];

/** A single KPI card: a label plus a declarative `op` over a named column. */
export interface KpiSpec {
  label: string;
  op: AllowedOp;
  column: string;
}

/** One chart series: a declarative `op` over a named column (no numbers). */
export interface SeriesSpec {
  column: string;
  op: AllowedOp;
}

/** A single chart: a type, a title, a group-by `xColumn`, and 1+ series. */
export interface ChartDef {
  type: ChartType;
  title: string;
  xColumn: string;
  series: SeriesSpec[];
}

/** The validated, strictly declarative auto-dashboard specification. */
export interface ChartSpec {
  kpis: KpiSpec[];
  charts: ChartDef[];
  insight?: string | null;
}

// ---------------------------------------------------------------------------
// Computed output types
// ---------------------------------------------------------------------------

/**
 * A KPI card with its value computed locally from the rows, or marked
 * uncomputable when it references an unknown column or a disallowed op.
 */
export interface ComputedKpi {
  label: string;
  op: AllowedOp;
  column: string;
  /** True iff the column exists and the op is allowed. */
  computable: boolean;
  /** The locally computed value; `null` when uncomputable or no data. */
  value: number | null;
  /** Human-readable reason present only when `computable` is false. */
  reason?: string;
}

/** One resolved chart series with a stable data key for the chart renderer. */
export interface ComputedSeries {
  column: string;
  op: AllowedOp;
  /** Stable key under which this series' value is stored on each data point. */
  key: string;
}

/**
 * One group in a chart: the group-by (`x`) value plus one computed value per
 * series, keyed by that series' {@link ComputedSeries.key}.
 */
export interface ComputedChartPoint {
  /** The raw group-by value from `xColumn`. */
  x: unknown;
  /** A stable string label for the group (used for axis/table display). */
  xLabel: string;
  /** Computed series values keyed by series key; `null` when no data. */
  [seriesKey: string]: unknown;
}

/**
 * A chart with its per-group series values computed locally from the rows, or
 * marked uncomputable when it references an unknown column or a disallowed op.
 */
export interface ComputedChart {
  type: ChartType;
  title: string;
  xColumn: string;
  /** True iff `xColumn` and every series column exist and every op is allowed. */
  computable: boolean;
  /** The resolved series (empty when uncomputable). */
  series: ComputedSeries[];
  /** One point per distinct `xColumn` value, in first-seen order. */
  data: ComputedChartPoint[];
  /** Human-readable reason present only when `computable` is false. */
  reason?: string;
}

/** The fully computed dashboard data derived locally from the Result_Set. */
export interface ComputedChartSpecData {
  kpis: ComputedKpi[];
  charts: ComputedChart[];
  /** The bounded, single-line insight (≤ 200 chars) or `null`. */
  insight: string | null;
}

// ---------------------------------------------------------------------------
// Aggregation primitives
// ---------------------------------------------------------------------------

/**
 * Coerce an arbitrary cell value to a finite number, or `null` when it is not
 * numeric. Accepts JS numbers and numeric strings; rejects `null`/`undefined`,
 * booleans, objects, `NaN`, and `Infinity`. This is how non-numeric values are
 * handled gracefully: they simply drop out of numeric aggregations.
 */
function toNumber(value: unknown): number | null {
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

/**
 * Apply a single allowed aggregation `op` over the given column across the
 * supplied rows, returning the locally computed number or `null` when there is
 * no data to compute from.
 *
 * - `count` counts non-null values of the column (SQL `COUNT(col)` semantics).
 * - `sum`/`avg`/`min`/`max` operate over the numeric subset of the column,
 *   ignoring non-numeric and null cells. `sum` of no numbers is `0`; the others
 *   return `null` when the numeric subset is empty.
 */
function aggregate(
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
    const n = toNumber(row[column]);
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
    default:
      return null;
  }
}

/** True iff `op` is one of the bounded allowed operations. */
function isAllowedOp(op: unknown): op is AllowedOp {
  return typeof op === "string" && (ALLOWED_OPS as readonly string[]).includes(op);
}

/** Render an arbitrary group-by value as a stable display string. */
function toLabel(value: unknown): string {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  return String(value);
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/**
 * Compute the concrete dashboard numbers for a validated `ChartSpec` from the
 * actual rows of its source `ResultSet`.
 *
 * Every KPI and chart is validated against `resultSet.columns` and the bounded
 * allowed op set. Computable KPIs/charts carry values derived locally from the
 * rows; uncomputable ones (unknown column or disallowed op) are marked with a
 * reason instead of throwing, so callers can omit and indicate them
 * (Requirement 10.4). The optional insight is collapsed to a single bounded
 * line (Requirement 10.5).
 */
export function computeChartSpecData(
  spec: ChartSpec,
  resultSet: ResultSet,
): ComputedChartSpecData {
  const columns = new Set(resultSet.columns);
  const rows = resultSet.rows;

  const kpis: ComputedKpi[] = (spec.kpis ?? []).map((kpi) =>
    computeKpi(kpi, columns, rows),
  );

  const charts: ComputedChart[] = (spec.charts ?? []).map((chart) =>
    computeChart(chart, columns, rows),
  );

  return {
    kpis,
    charts,
    insight: boundedInsight(spec.insight),
  };
}

/** Compute a single KPI card, marking it uncomputable when invalid. */
function computeKpi(
  kpi: KpiSpec,
  columns: ReadonlySet<string>,
  rows: ReadonlyArray<Record<string, unknown>>,
): ComputedKpi {
  const base = { label: kpi.label, op: kpi.op, column: kpi.column };

  if (!isAllowedOp(kpi.op)) {
    return {
      ...base,
      computable: false,
      value: null,
      reason: `Unsupported operation "${String(kpi.op)}".`,
    };
  }
  if (!columns.has(kpi.column)) {
    return {
      ...base,
      computable: false,
      value: null,
      reason: `Unknown column "${kpi.column}".`,
    };
  }

  return {
    ...base,
    computable: true,
    value: aggregate(rows, kpi.column, kpi.op),
  };
}

/** Compute a single chart, marking it uncomputable when invalid. */
function computeChart(
  chart: ChartDef,
  columns: ReadonlySet<string>,
  rows: ReadonlyArray<Record<string, unknown>>,
): ComputedChart {
  const base = {
    type: chart.type,
    title: chart.title,
    xColumn: chart.xColumn,
  };

  if (!columns.has(chart.xColumn)) {
    return {
      ...base,
      computable: false,
      series: [],
      data: [],
      reason: `Unknown group-by column "${chart.xColumn}".`,
    };
  }

  const seriesList = chart.series ?? [];
  for (const s of seriesList) {
    if (!isAllowedOp(s.op)) {
      return {
        ...base,
        computable: false,
        series: [],
        data: [],
        reason: `Unsupported operation "${String(s.op)}".`,
      };
    }
    if (!columns.has(s.column)) {
      return {
        ...base,
        computable: false,
        series: [],
        data: [],
        reason: `Unknown column "${s.column}".`,
      };
    }
  }

  const series: ComputedSeries[] = seriesList.map((s, index) => ({
    column: s.column,
    op: s.op,
    key: `series_${index}`,
  }));

  // Group rows by the xColumn value, preserving first-seen order.
  const groupOrder: unknown[] = [];
  const groups = new Map<string, { x: unknown; rows: Record<string, unknown>[] }>();
  for (const row of rows) {
    const x = row[chart.xColumn];
    const groupKey = `${typeof x}:${toLabel(x)}`;
    let group = groups.get(groupKey);
    if (group === undefined) {
      group = { x, rows: [] };
      groups.set(groupKey, group);
      groupOrder.push(groupKey);
    }
    group.rows.push(row);
  }

  const data: ComputedChartPoint[] = groupOrder.map((groupKey) => {
    const group = groups.get(groupKey as string)!;
    const point: ComputedChartPoint = {
      x: group.x,
      xLabel: toLabel(group.x),
    };
    for (const s of series) {
      point[s.key] = aggregate(group.rows, s.column, s.op);
    }
    return point;
  });

  return {
    ...base,
    computable: true,
    series,
    data,
  };
}
