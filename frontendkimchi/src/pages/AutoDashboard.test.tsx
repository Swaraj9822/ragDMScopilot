import { cloneElement, type ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { axe, toHaveNoViolations } from "jest-axe";
import type { ResultSet } from "../api/sqlLab";
import type { AllowedOp, ChartSpec } from "./computeChartSpecData";

expect.extend(toHaveNoViolations);

// ---------------------------------------------------------------------------
// recharts' ResponsiveContainer measures its parent with a ResizeObserver and
// renders nothing at the 0×0 size jsdom reports, which would keep the chart
// children from mounting. Mock it to a fixed size and forward concrete
// width/height to the chart element so the SVG renders deterministically. The
// data-table equivalent these tests assert on lives OUTSIDE the container, so
// this only stabilizes the (aria-hidden) visual chart.
// ---------------------------------------------------------------------------
vi.mock("recharts", async (importActual) => {
  const actual = await importActual<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactElement }) => (
      <div style={{ width: 800, height: 300 }}>
        {cloneElement(children, { width: 800, height: 300 })}
      </div>
    ),
  };
});

// Import after the mock is registered so the component picks up mocked recharts.
import { AutoDashboard } from "./AutoDashboard";

function makeResultSet(overrides: Partial<ResultSet> = {}): ResultSet {
  return {
    columns: ["region", "amount"],
    rows: [
      { region: "North", amount: 10 },
      { region: "North", amount: 20 },
      { region: "South", amount: 5 },
    ],
    rowCount: 3,
    durationMs: 4,
    sql: "SELECT region, amount FROM sales",
    truncated: false,
    ...overrides,
  };
}

describe("AutoDashboard", () => {
  it("renders KPI cards with values computed locally from the rows (R10.1)", () => {
    const spec: ChartSpec = {
      // sum(amount) over 10 + 20 + 5 = 35; count(region) over 3 non-null = 3.
      kpis: [
        { label: "Total amount", op: "sum", column: "amount" },
        { label: "Region rows", op: "count", column: "region" },
      ],
      charts: [],
    };

    render(<AutoDashboard spec={spec} resultSet={makeResultSet()} />);

    // The displayed KPI value equals the locally computed aggregate, not a
    // number emitted by the model.
    expect(screen.getByText("Total amount")).toBeInTheDocument();
    expect(screen.getByText("35")).toBeInTheDocument();
    expect(screen.getByText("Region rows")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    // The declarative op/column is surfaced on the card.
    expect(screen.getByText("sum(amount)")).toBeInTheDocument();
    expect(screen.getByText("count(region)")).toBeInTheDocument();
  });

  it("renders 1–3 charts each with a keyboard-reachable, associated data table showing locally computed per-group values, and passes an axe smoke check (R10.6)", async () => {
    const spec: ChartSpec = {
      kpis: [{ label: "Total amount", op: "sum", column: "amount" }],
      charts: [
        {
          type: "bar",
          title: "Revenue by region",
          xColumn: "region",
          series: [{ column: "amount", op: "sum" }],
        },
      ],
    };

    const { container } = render(
      <AutoDashboard spec={spec} resultSet={makeResultSet()} />,
    );

    // Exactly one chart is rendered (between 1 and 3 inclusive, R10.1).
    const figures = screen.getAllByRole("group", { name: "Revenue by region" });
    expect(figures).toHaveLength(1);
    const figure = figures[0];

    // The chart is programmatically associated with its data table via
    // aria-describedby (R10.6).
    const tableId = figure.getAttribute("aria-describedby");
    expect(tableId).toBeTruthy();
    const table = document.getElementById(tableId!);
    expect(table).not.toBeNull();
    expect(table!.tagName).toBe("TABLE");

    // The data table is reachable via a keyboard-operable details/summary
    // disclosure (native <summary> is focusable/activatable).
    expect(
      screen.getByText(/show data table for “revenue by region”/i),
    ).toBeInTheDocument();

    // The table presents the same data points, computed locally per group:
    // North → sum(amount) = 30, South → sum(amount) = 5.
    const dataTable = table as HTMLTableElement;
    expect(within(dataTable).getByText("region")).toBeInTheDocument();
    expect(within(dataTable).getByText("sum(amount)")).toBeInTheDocument();
    const northRow = within(dataTable).getByText("North").closest("tr")!;
    expect(within(northRow).getByText("30")).toBeInTheDocument();
    const southRow = within(dataTable).getByText("South").closest("tr")!;
    expect(within(southRow).getByText("5")).toBeInTheDocument();

    // Accessibility smoke check over the whole dashboard subtree.
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });

  it("omits uncomputable KPIs/charts from the values and surfaces a 'could not be computed' note (R10.4)", () => {
    const spec: ChartSpec = {
      kpis: [
        { label: "Total amount", op: "sum", column: "amount" },
        // Disallowed op: not in the bounded allowed set → uncomputable.
        {
          label: "Median amount",
          op: "median" as unknown as AllowedOp,
          column: "amount",
        },
        // Unknown column → uncomputable.
        { label: "Ghost metric", op: "sum", column: "ghost" },
      ],
      charts: [
        // Unknown group-by column → uncomputable chart.
        {
          type: "bar",
          title: "Broken chart",
          xColumn: "ghost",
          series: [{ column: "amount", op: "sum" }],
        },
      ],
    };

    render(<AutoDashboard spec={spec} resultSet={makeResultSet()} />);

    // The valid KPI still renders its locally computed value.
    expect(screen.getByText("Total amount")).toBeInTheDocument();
    expect(screen.getByText("35")).toBeInTheDocument();

    // Uncomputable items are not rendered as value cards / chart figures.
    expect(screen.queryByText("median(amount)")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: "Broken chart" }),
    ).not.toBeInTheDocument();

    // Each uncomputable item is surfaced with a "could not be computed" note.
    const notes = screen.getByRole("status");
    expect(
      within(notes).getByText(/median amount.*could not be computed/i),
    ).toBeInTheDocument();
    expect(
      within(notes).getByText(/ghost metric.*could not be computed/i),
    ).toBeInTheDocument();
    expect(
      within(notes).getByText(/broken chart.*could not be computed/i),
    ).toBeInTheDocument();
  });

  it("renders an empty state when the spec yields zero chartable data points (R10.8)", () => {
    const spec: ChartSpec = {
      // No KPIs, and the single chart is valid but there are no rows, so it
      // produces zero data points → nothing chartable.
      kpis: [],
      charts: [
        {
          type: "bar",
          title: "Revenue by region",
          xColumn: "region",
          series: [{ column: "amount", op: "sum" }],
        },
      ],
    };

    render(
      <AutoDashboard
        spec={spec}
        resultSet={makeResultSet({ rows: [], rowCount: 0 })}
      />,
    );

    // The empty state is shown...
    expect(screen.getByText(/no chartable data/i)).toBeInTheDocument();
    // ...and no chart figure is rendered.
    expect(
      screen.queryByRole("group", { name: "Revenue by region" }),
    ).not.toBeInTheDocument();
  });

  // R10.7 (analysis failure keeps the underlying Result_Set rows visible) is a
  // SqlLabPage-level concern: AutoDashboard is only mounted with a validated
  // spec and renders below the results table without touching it. That path is
  // exercised in SqlLabPage.test.tsx (error state + retained rows), so it is
  // intentionally not duplicated here.
});
