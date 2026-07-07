import { useId, useMemo } from "react";
import { AlertTriangle, BarChart3 } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { EmptyState } from "../components/common/EmptyState";
import type { ResultSet } from "../api/sqlLab";
import {
  computeChartSpecData,
  type ChartSpec,
  type ComputedChart,
  type ComputedKpi,
} from "./computeChartSpecData";
import styles from "./AutoDashboard.module.css";

interface AutoDashboardProps {
  /** The validated, strictly declarative Chart_Spec from `POST /sql/analyze`. */
  spec: ChartSpec;
  /** The source Result_Set whose rows every displayed value is computed from. */
  resultSet: ResultSet;
}

/**
 * A qualitative palette for chart series, pie slices and per-category bars.
 * Anchored on the app accent and extended with distinct, legible hues so a
 * single-series categorical chart is no longer a monotone wall of one color.
 */
const SERIES_COLORS = [
  "#43d6b5", // accent teal
  "#79a9ff", // info blue
  "#f4b740", // amber
  "#c084fc", // violet
  "#f87171", // red
  "#4ade80", // green
  "#22d3ee", // cyan
  "#fb923c", // orange
];

/** Neutral slate that reads acceptably on both the dark and light themes. */
const AXIS_COLOR = "#8b97a4";
const GRID_COLOR = "rgba(148, 163, 184, 0.16)";

/**
 * Format a locally computed numeric value at full precision. Whole numbers
 * render without decimals; fractional values are rounded to at most three
 * decimals. `null` (no data / uncomputable) renders as an em dash. Used by the
 * data-table equivalent and tooltips where exact figures matter.
 */
function formatValue(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "—";
  }
  if (Number.isInteger(value)) {
    return value.toLocaleString();
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

/**
 * Format a value compactly for headline display (KPI cards, axis ticks) so
 * large aggregates fit their container: 82_727_786.9 → "82.73M". Small values
 * (< 10_000) keep their full form so precision is not lost where it fits.
 */
function formatCompact(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "—";
  }
  if (Math.abs(value) >= 10_000) {
    return value.toLocaleString(undefined, {
      notation: "compact",
      maximumFractionDigits: 2,
    });
  }
  return formatValue(value);
}

/** A human-readable series label, e.g. `sum(amount)`. */
function seriesLabel(op: string, column: string): string {
  return `${op}(${column})`;
}

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

interface TooltipEntry {
  name?: string;
  value?: number | string | null;
  color?: string;
}

/**
 * A themed tooltip replacing recharts' bright default box, so hovered values
 * are legible on the dark surface and share the dashboard's visual language.
 */
function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
}) {
  if (!active || !payload || payload.length === 0) {
    return null;
  }
  return (
    <div className={styles.tooltip}>
      {label !== undefined && label !== "" && (
        <p className={styles.tooltipLabel}>{label}</p>
      )}
      {payload.map((entry, index) => (
        <p key={`${entry.name}-${index}`} className={styles.tooltipRow}>
          <span
            className={styles.tooltipDot}
            style={{ background: entry.color }}
            aria-hidden="true"
          />
          <span className={styles.tooltipName}>{entry.name}</span>
          <span className={styles.tooltipValue}>
            {formatValue(
              typeof entry.value === "number" ? entry.value : null,
            )}
          </span>
        </p>
      ))}
    </div>
  );
}

/**
 * The AI auto-dashboard (Slice 4). Receives a validated declarative Chart_Spec
 * plus the source Result_Set and renders KPI cards and 1–3 charts whose every
 * numeric value is computed locally from the actual returned rows via
 * {@link computeChartSpecData} (R10.1, R10.3) — the language model never emits a
 * number, so none can be fabricated.
 *
 * KPIs and charts that reference an unknown column or a disallowed operation are
 * omitted from the visual output and surfaced as an explicit "could not be
 * computed" note (R10.4). Each chart carries a keyboard-reachable, semantically
 * associated data table equivalent presenting the same data points (R10.6). When
 * the spec yields zero chartable data points, an empty state is shown (R10.8);
 * in every case the caller keeps the underlying Result_Set rows visible (R10.7,
 * R10.8).
 */
export function AutoDashboard({ spec, resultSet }: AutoDashboardProps) {
  const computed = useMemo(
    () => computeChartSpecData(spec, resultSet),
    [spec, resultSet],
  );

  const computableKpis = computed.kpis.filter((k) => k.computable);
  const uncomputableKpis = computed.kpis.filter((k) => !k.computable);
  // A chart is chartable only when it resolved AND produced at least one point.
  const chartableCharts = computed.charts.filter(
    (c) => c.computable && c.data.length > 0,
  );
  const uncomputableCharts = computed.charts.filter((c) => !c.computable);

  const hasChartableData =
    computableKpis.length > 0 || chartableCharts.length > 0;

  // R10.8: a validated spec that yields zero chartable data points shows an
  // empty state. The caller keeps the underlying rows visible regardless.
  if (!hasChartableData) {
    return (
      <section className={styles.dashboard} aria-label="Auto dashboard">
        <header className={styles.header}>
          <h2 className={styles.heading}>
            <BarChart3 size={16} aria-hidden="true" />
            Auto dashboard
          </h2>
        </header>
        <EmptyState
          icon={BarChart3}
          title="No chartable data"
          body="The analysis produced no values that could be computed from these rows."
        />
        {(uncomputableKpis.length > 0 || uncomputableCharts.length > 0) && (
          <UncomputableNotes kpis={uncomputableKpis} charts={uncomputableCharts} />
        )}
      </section>
    );
  }

  return (
    <section className={styles.dashboard} aria-label="Auto dashboard">
      <header className={styles.header}>
        <h2 className={styles.heading}>
          <BarChart3 size={16} aria-hidden="true" />
          Auto dashboard
        </h2>
        {/* At most one insight line, ≤ 200 chars (R10.5). */}
        {computed.insight && <p className={styles.insight}>{computed.insight}</p>}
      </header>

      {/* KPI cards — only computable ones are shown as values (R10.4). */}
      {computableKpis.length > 0 && (
        <div className={styles.kpiGrid}>
          {computableKpis.map((kpi, index) => (
            <div
              key={`${kpi.label}-${index}`}
              className={styles.kpiCard}
              style={{
                // A thin accent rail cycles through the palette so adjacent
                // cards are visually distinct and scannable.
                ["--kpi-accent" as string]:
                  SERIES_COLORS[index % SERIES_COLORS.length],
              }}
            >
              <span className={styles.kpiLabel} title={kpi.label}>
                {kpi.label}
              </span>
              <span
                className={styles.kpiValue}
                title={formatValue(kpi.value)}
              >
                {formatCompact(kpi.value)}
              </span>
              <span className={styles.kpiOp}>{seriesLabel(kpi.op, kpi.column)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Charts — 1..3 rendered from locally computed aggregates (R10.1). */}
      {chartableCharts.length > 0 && (
        <div className={styles.charts}>
          {chartableCharts.slice(0, 3).map((chart, index) => (
            <ChartBlock key={`${chart.title}-${index}`} chart={chart} />
          ))}
        </div>
      )}

      {/* Omitted KPIs/charts are surfaced explicitly (R10.4). */}
      {(uncomputableKpis.length > 0 || uncomputableCharts.length > 0) && (
        <UncomputableNotes kpis={uncomputableKpis} charts={uncomputableCharts} />
      )}
    </section>
  );
}

/** Notes listing KPIs/charts that could not be computed (R10.4). */
function UncomputableNotes({
  kpis,
  charts,
}: {
  kpis: ComputedKpi[];
  charts: ComputedChart[];
}) {
  return (
    <div className={styles.notes} role="status">
      {kpis.map((kpi, index) => (
        <p key={`kpi-${kpi.label}-${index}`} className={styles.uncomputableNote}>
          <AlertTriangle size={14} aria-hidden="true" />
          KPI “{kpi.label}” could not be computed. {kpi.reason}
        </p>
      ))}
      {charts.map((chart, index) => (
        <p
          key={`chart-${chart.title}-${index}`}
          className={styles.uncomputableNote}
        >
          <AlertTriangle size={14} aria-hidden="true" />
          Chart “{chart.title}” could not be computed. {chart.reason}
        </p>
      ))}
    </div>
  );
}

/**
 * Render a single chart plus its keyboard-reachable, programmatically
 * associated data table equivalent (R10.6). The SVG chart is `aria-hidden`
 * because the table is its accessible equivalent, and the chart figure is
 * associated with the table via `aria-describedby`.
 */
function ChartBlock({ chart }: { chart: ComputedChart }) {
  const titleId = useId();
  const tableId = useId();

  return (
    <figure
      className={styles.chart}
      role="group"
      aria-labelledby={titleId}
      aria-describedby={tableId}
    >
      <figcaption id={titleId} className={styles.chartTitle}>
        {chart.title}
      </figcaption>

      <div className={styles.chartCanvas} aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          {renderChart(chart)}
        </ResponsiveContainer>
      </div>

      {/* Keyboard-reachable, semantically associated data equivalent (R10.6). */}
      <details className={styles.details}>
        <summary>Show data table for “{chart.title}”</summary>
        <div className={styles.dataTableWrap}>
          <table id={tableId} className={styles.dataTable}>
            <caption>Data for {chart.title}</caption>
            <thead>
              <tr>
                <th scope="col">{chart.xColumn}</th>
                {chart.series.map((s) => (
                  <th key={s.key} scope="col">
                    {seriesLabel(s.op, s.column)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {chart.data.map((point, rowIndex) => (
                <tr key={rowIndex}>
                  <th scope="row">{point.xLabel}</th>
                  {chart.series.map((s) => (
                    <td key={s.key}>
                      {formatValue((point[s.key] as number | null) ?? null)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

/**
 * Build the recharts element for a computed chart. Bar/line charts render every
 * series; a pie chart renders its first series' value per group. Axes, grid and
 * tooltip share a themed, low-contrast style, and a single-series bar chart
 * colors each category distinctly so the shape of the data is easier to read.
 */
function renderChart(chart: ComputedChart) {
  if (chart.type === "pie") {
    const series = chart.series[0];
    const pieData = series
      ? chart.data.map((point) => ({
          name: point.xLabel,
          value: (point[series.key] as number | null) ?? 0,
        }))
      : [];
    return (
      <PieChart>
        <Tooltip content={<ChartTooltip />} />
        <Legend iconType="circle" />
        <Pie
          data={pieData}
          dataKey="value"
          nameKey="name"
          innerRadius="45%"
          outerRadius="78%"
          paddingAngle={2}
          stroke="var(--bg-surface)"
          strokeWidth={2}
        >
          {pieData.map((_, index) => (
            <Cell
              key={index}
              fill={SERIES_COLORS[index % SERIES_COLORS.length]}
            />
          ))}
        </Pie>
      </PieChart>
    );
  }

  if (chart.type === "line") {
    return (
      <LineChart data={chart.data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis
          dataKey="xLabel"
          tick={{ fill: AXIS_COLOR, fontSize: 11 }}
          tickLine={false}
          axisLine={{ stroke: GRID_COLOR }}
        />
        <YAxis
          tick={{ fill: AXIS_COLOR, fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          width={56}
          tickFormatter={(v: number) => formatCompact(v)}
        />
        <Tooltip content={<ChartTooltip />} />
        {chart.series.length > 1 && <Legend iconType="line" />}
        {chart.series.map((s, index) => (
          <Line
            key={s.key}
            type="monotone"
            dataKey={s.key}
            name={seriesLabel(s.op, s.column)}
            stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
            strokeWidth={2}
            dot={{ r: 3, strokeWidth: 0 }}
            activeDot={{ r: 5 }}
          />
        ))}
      </LineChart>
    );
  }

  // Default: bar chart.
  const singleSeries = chart.series.length === 1;
  return (
    <BarChart data={chart.data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
      <CartesianGrid stroke={GRID_COLOR} vertical={false} />
      <XAxis
        dataKey="xLabel"
        tick={{ fill: AXIS_COLOR, fontSize: 11 }}
        tickLine={false}
        axisLine={{ stroke: GRID_COLOR }}
        interval={0}
      />
      <YAxis
        tick={{ fill: AXIS_COLOR, fontSize: 11 }}
        tickLine={false}
        axisLine={false}
        width={56}
        tickFormatter={(v: number) => formatCompact(v)}
      />
      <Tooltip
        content={<ChartTooltip />}
        cursor={{ fill: "rgba(148, 163, 184, 0.08)" }}
      />
      {!singleSeries && <Legend iconType="circle" />}
      {chart.series.map((s, index) => (
        <Bar
          key={s.key}
          dataKey={s.key}
          name={seriesLabel(s.op, s.column)}
          fill={SERIES_COLORS[index % SERIES_COLORS.length]}
          radius={[6, 6, 0, 0]}
          maxBarSize={64}
        >
          {/* When there is a single series, color each category distinctly so
              the bars are no longer a monotone block. */}
          {singleSeries &&
            chart.data.map((_, i) => (
              <Cell key={i} fill={SERIES_COLORS[i % SERIES_COLORS.length]} />
            ))}
        </Bar>
      ))}
    </BarChart>
  );
}

export default AutoDashboard;
