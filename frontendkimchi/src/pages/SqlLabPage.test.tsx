import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe, toHaveNoViolations } from "jest-axe";
import { ApiError } from "../api/client";
import type { ResultSet, SchemaTable } from "../api/sqlLab";
import { renderWithProviders } from "../test/renderWithProviders";

expect.extend(toHaveNoViolations);

// ---------------------------------------------------------------------------
// Mock the typed API layer. These tests exercise the page's state machine,
// honest states, and accessibility — not the network — so `runSql` and
// `listSchema` are mocked and driven directly (success with/without rows,
// truncated, guard/db/auth errors; loaded/empty/error schema states).
// ---------------------------------------------------------------------------
const runSqlMock = vi.hoisted(() => vi.fn());
const listSchemaMock = vi.hoisted(() => vi.fn());
vi.mock("../api/sqlLab", () => ({
  runSql: runSqlMock,
  listSchema: listSchemaMock,
}));

// Import after the mock is registered so the page picks up the mocked module.
import SqlLabPage from "./SqlLabPage";

/** A promise whose resolution is controlled by the test (for in-flight states). */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeResultSet(overrides: Partial<ResultSet> = {}): ResultSet {
  return {
    columns: ["id", "name"],
    rows: [
      { id: 1, name: "alice" },
      { id: 2, name: "bob" },
    ],
    rowCount: 2,
    durationMs: 12,
    sql: "SELECT id, name FROM t",
    truncated: false,
    ...overrides,
  };
}

const SQL = "SELECT id, name FROM t";

async function typeQuery(user: ReturnType<typeof userEvent.setup>, sql = SQL) {
  const editor = screen.getByLabelText(/sql query/i);
  await user.clear(editor);
  await user.type(editor, sql);
  return editor as HTMLTextAreaElement;
}

beforeEach(() => {
  runSqlMock.mockReset();
  // Sensible default so the sidebar's on-mount schema load resolves cleanly for
  // the tests that don't care about the schema (they exercise the query flow).
  listSchemaMock.mockReset();
  listSchemaMock.mockResolvedValue([]);
});

describe("SqlLabPage", () => {
  it("shows the Skeleton loading state immediately and hides any prior result (R6.1)", async () => {
    const user = userEvent.setup();
    // First run resolves with rows so a prior result is on screen.
    runSqlMock.mockResolvedValueOnce(makeResultSet());
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));
    // Prior result rendered.
    expect(await screen.findByText("alice")).toBeInTheDocument();

    // Second run is left in flight so the loading state is observable.
    const pending = deferred<ResultSet>();
    runSqlMock.mockReturnValueOnce(pending.promise);
    await user.click(screen.getByRole("button", { name: "Run" }));

    // The Skeleton is displayed (well within the 200 ms budget — the loading
    // state is dispatched synchronously before the request is awaited)...
    expect(screen.getByTestId("sql-lab-skeleton")).toBeInTheDocument();
    // ...and the prior result set is no longer shown (no raw/blank/stale view).
    expect(screen.queryByText("alice")).not.toBeInTheDocument();

    pending.resolve(makeResultSet());
    await screen.findByText("alice");
  });

  it("renders the EmptyState (not the ErrorState) when a query returns zero rows (R6.2)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockResolvedValueOnce(
      makeResultSet({ rows: [], rowCount: 0, columns: [] }),
    );
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText(/no rows returned/i)).toBeInTheDocument();
    // The empty state is visually and semantically distinct from the error
    // state: no alert role is present on success.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the returned rows in the results table on success (R5.5)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockResolvedValueOnce(makeResultSet());
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    const results = await screen.findByRole("region", { name: /query results/i });
    expect(within(results).getByText("alice")).toBeInTheDocument();
    expect(within(results).getByText("bob")).toBeInTheDocument();
  });

  it("shows the guard rejection message in the ErrorState and retains the SQL (R6.3)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockRejectedValueOnce(
      new ApiError(400, "WITH clause is not permitted in version 1."),
    );
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    const editor = await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/query failed/i)).toBeInTheDocument();
    expect(
      within(alert).getByText(/with clause is not permitted/i),
    ).toBeInTheDocument();
    // The submitted statement is retained for correction and resubmission.
    expect(editor).toHaveValue(SQL);
  });

  it("shows a database/timeout error message in the ErrorState and retains the SQL (R6.4)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockRejectedValueOnce(
      new ApiError(504, "Statement timeout of 10000 ms exceeded."),
    );
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    const editor = await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/statement timeout/i)).toBeInTheDocument();
    expect(editor).toHaveValue(SQL);
  });

  it("shows the operator-restricted message for auth failures (R6.5)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockRejectedValueOnce(new ApiError(403, "operator_required"));
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText(/sql lab access is restricted to operators/i),
    ).toBeInTheDocument();
  });

  it("disables Run when the editor is empty/whitespace and while a request is in flight (R5.4, R5.7)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    // Empty editor → Run disabled and a non-empty-query hint is visible.
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByText(/enter a non-empty query to run/i)).toBeInTheDocument();

    // Whitespace-only → still disabled.
    const editor = screen.getByLabelText(/sql query/i);
    await user.type(editor, "   ");
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();

    // Non-empty → enabled.
    await user.clear(editor);
    await user.type(editor, SQL);
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();

    // In flight → disabled (label switches to "Running…").
    const pending = deferred<ResultSet>();
    runSqlMock.mockReturnValueOnce(pending.promise);
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(screen.getByRole("button", { name: /running/i })).toBeDisabled();

    pending.resolve(makeResultSet());
    await screen.findByText("alice");
  });

  it("displays a persistent truncation banner when the result is truncated (R5.6)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockResolvedValueOnce(
      makeResultSet({ truncated: true, rowCount: 2 }),
    );
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    const banner = await screen.findByText(/results were limited to the first 2 rows/i);
    expect(banner).toBeInTheDocument();
    // It remains visible for as long as the results are shown.
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(banner).toBeInTheDocument();
  });

  it("announces begin and success through a polite aria-live region (R5.9)", async () => {
    const user = userEvent.setup();
    const pending = deferred<ResultSet>();
    runSqlMock.mockReturnValueOnce(pending.promise);
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    // Begin announcement.
    const liveRegion = await screen.findByText(/running query/i);
    expect(liveRegion).toHaveAttribute("aria-live", "polite");

    // Success announcement.
    pending.resolve(makeResultSet({ rowCount: 2 }));
    await waitFor(() =>
      expect(
        screen.getByText(/query succeeded and returned 2 rows/i),
      ).toBeInTheDocument(),
    );
  });

  it("announces failure through the polite aria-live region (R5.9)", async () => {
    const user = userEvent.setup();
    runSqlMock.mockRejectedValueOnce(new ApiError(400, "boom"));
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() =>
      expect(screen.getByText(/query failed: boom/i)).toBeInTheDocument(),
    );
  });

  it("has no detectable accessibility violations (R5.8) — axe smoke check", async () => {
    const user = userEvent.setup();
    runSqlMock.mockResolvedValueOnce(makeResultSet());
    const { container } = renderWithProviders(<SqlLabPage />, {
      route: "/sql-lab",
    });

    // Run a query so the results table and meta are also covered by the scan.
    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText("alice");

    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});

// ---------------------------------------------------------------------------
// Schema sidebar (Slice 2 — task 10.7). Covers the three honest states the
// sidebar renders from the on-mount `listSchema` call: loaded (tables +
// columns), empty (no tables), and error (failed request → no table list).
// ---------------------------------------------------------------------------

function makeSchema(): SchemaTable[] {
  return [
    {
      name: "orders",
      columns: [
        { name: "id", type: "integer" },
        { name: "total", type: "numeric" },
      ],
    },
    {
      name: "customers",
      columns: [{ name: "email", type: "text" }],
    },
  ];
}

describe("SqlLabPage schema sidebar", () => {
  it("renders each returned table and its columns when the Schema panel is expanded (R7.5)", async () => {
    const user = userEvent.setup();
    listSchemaMock.mockResolvedValueOnce(makeSchema());
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    const sidebar = screen.getByRole("complementary", { name: /sql lab tools/i });
    // The Schema panel is collapsed by default; expand it by clicking its button.
    await user.click(within(sidebar).getByRole("button", { name: /^schema$/i }));

    // Each returned table name is rendered.
    expect(await within(sidebar).findByText("orders")).toBeInTheDocument();
    expect(within(sidebar).getByText("customers")).toBeInTheDocument();

    // Each table's columns (name + type) are rendered.
    expect(within(sidebar).getByText("id")).toBeInTheDocument();
    expect(within(sidebar).getByText("total")).toBeInTheDocument();
    expect(within(sidebar).getByText("email")).toBeInTheDocument();
    expect(within(sidebar).getAllByText("integer").length).toBeGreaterThan(0);
    expect(within(sidebar).getByText("numeric")).toBeInTheDocument();
    expect(within(sidebar).getByText("text")).toBeInTheDocument();

    // Not the empty or error indication.
    expect(
      within(sidebar).queryByText(/no tables are available/i),
    ).not.toBeInTheDocument();
  });

  it("shows the no-tables indication when zero tables are returned (R7.6)", async () => {
    const user = userEvent.setup();
    listSchemaMock.mockResolvedValueOnce([]);
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    const sidebar = screen.getByRole("complementary", { name: /sql lab tools/i });
    await user.click(within(sidebar).getByRole("button", { name: /^schema$/i }));

    expect(
      await within(sidebar).findByText(/no tables are available/i),
    ).toBeInTheDocument();
    // No table list is rendered in the empty state.
    expect(within(sidebar).queryByRole("list")).not.toBeInTheDocument();
  });

  it("shows an error indication and does NOT render a table list when the request fails (R7.7)", async () => {
    const user = userEvent.setup();
    listSchemaMock.mockRejectedValueOnce(
      new ApiError(500, "The schema listing could not be retrieved."),
    );
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    const sidebar = screen.getByRole("complementary", { name: /sql lab tools/i });
    await user.click(within(sidebar).getByRole("button", { name: /^schema$/i }));

    // The backend-supplied error detail is shown as the error indication.
    expect(
      await within(sidebar).findByText(/schema listing could not be retrieved/i),
    ).toBeInTheDocument();

    // No table list is rendered on error (R7.7): neither a list nor any table
    // that would have come from a partial/stale response.
    expect(within(sidebar).queryByRole("list")).not.toBeInTheDocument();
    expect(within(sidebar).queryByText("orders")).not.toBeInTheDocument();
    expect(
      within(sidebar).queryByText(/no tables are available/i),
    ).not.toBeInTheDocument();
  });
});
