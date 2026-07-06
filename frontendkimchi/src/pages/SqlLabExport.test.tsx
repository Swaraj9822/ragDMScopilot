import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ResultSet } from "../api/sqlLab";
import { renderWithProviders } from "../test/renderWithProviders";

// ---------------------------------------------------------------------------
// These tests exercise the SqlLabPage CSV-export and query-history UI wiring
// (task 15.7), covering:
//   - R11.2: export blocked (with a notice) when there is no/zero-row result,
//     leaving the current view unchanged.
//   - R11.4: a history persist failure surfaces a notice WITHOUT discarding the
//     Result_Set.
//   - R11.5: selecting a history entry replaces the entire editor content with
//     the stored SQL.
//
// The typed API layer (`runSql`/`listSchema`) and the query-history module
// (`pushHistory`/`readHistory`) are mocked so the page's behaviour is driven
// directly. `queryHistory` is mocked in its own file (rather than extending
// SqlLabPage.test.tsx) so the real history persistence used by the existing
// success-path tests stays intact.
// ---------------------------------------------------------------------------
const runSqlMock = vi.hoisted(() => vi.fn());
const listSchemaMock = vi.hoisted(() => vi.fn());
const pushHistoryMock = vi.hoisted(() => vi.fn());
const readHistoryMock = vi.hoisted(() => vi.fn());

vi.mock("../api/sqlLab", () => ({
  runSql: runSqlMock,
  listSchema: listSchemaMock,
}));

vi.mock("./queryHistory", () => ({
  pushHistory: pushHistoryMock,
  readHistory: readHistoryMock,
  QUERY_HISTORY_LIMIT: 50,
  QUERY_HISTORY_KEY: "sql-lab-history",
}));

// Import after the mocks are registered so the page picks up the mocked modules.
import SqlLabPage from "./SqlLabPage";

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
  listSchemaMock.mockReset();
  listSchemaMock.mockResolvedValue([]);
  pushHistoryMock.mockReset();
  // Successful persistence returns the updated newest-first list by default.
  pushHistoryMock.mockImplementation((sql: string) => [sql]);
  readHistoryMock.mockReset();
  // Empty history by default so the history section is not rendered.
  readHistoryMock.mockReturnValue([]);
});

describe("SqlLabPage CSV export (R11.2)", () => {
  it("blocks export and shows a no-data notice when no result set is present, leaving the view unchanged", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    // Idle: no results region is present before export.
    expect(
      screen.queryByRole("region", { name: /query results/i }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByTestId("sql-lab-export"));

    // The export is blocked with a message and the view is unchanged: still no
    // results region, and no CSV download attempted.
    expect(screen.getByText(/there is no data to export/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: /query results/i }),
    ).not.toBeInTheDocument();
  });

  it("blocks export and shows the no-data notice when the current result set has zero rows", async () => {
    const user = userEvent.setup();
    // Drive an empty result via the runSql mock (zero rows / rowCount 0).
    runSqlMock.mockResolvedValueOnce(
      makeResultSet({ rows: [], rowCount: 0, columns: [] }),
    );
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    // The empty state is shown after the zero-row query.
    expect(await screen.findByText(/no rows returned/i)).toBeInTheDocument();

    await user.click(screen.getByTestId("sql-lab-export"));

    // Export is blocked with the notice; the empty state remains unchanged.
    expect(screen.getByText(/there is no data to export/i)).toBeInTheDocument();
    expect(screen.getByText(/no rows returned/i)).toBeInTheDocument();
  });
});

describe("SqlLabPage query-history persist failure (R11.4)", () => {
  it("shows a history-could-not-be-saved notice without discarding the Result_Set", async () => {
    const user = userEvent.setup();
    runSqlMock.mockResolvedValueOnce(makeResultSet());
    // Persisting history throws (e.g. localStorage quota exceeded).
    pushHistoryMock.mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    await typeQuery(user);
    await user.click(screen.getByRole("button", { name: "Run" }));

    // The run completes normally: the returned rows are still displayed.
    const results = await screen.findByRole("region", { name: /query results/i });
    expect(within(results).getByText("alice")).toBeInTheDocument();
    expect(within(results).getByText("bob")).toBeInTheDocument();

    // ...and the persist-failure notice is shown alongside the retained result.
    expect(
      screen.getByText(/query history could not be saved/i),
    ).toBeInTheDocument();
  });
});

describe("SqlLabPage history-entry selection (R11.5)", () => {
  it("replaces the entire editor content with the stored SQL of the selected entry", async () => {
    const user = userEvent.setup();
    const stored = "SELECT * FROM orders";
    readHistoryMock.mockReturnValue([stored]);
    renderWithProviders(<SqlLabPage />, { route: "/sql-lab" });

    // Seed the editor with different content to prove a full replacement.
    const editor = await typeQuery(user, "SELECT 1");
    expect(editor).toHaveValue("SELECT 1");

    const historySection = screen.getByRole("region", { name: /query history/i });
    await user.click(within(historySection).getByRole("button", { name: stored }));

    // The editor content is fully replaced with the stored statement.
    expect(editor).toHaveValue(stored);
  });
});
