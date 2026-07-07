import { Suspense, lazy, useEffect, useReducer, useRef, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  ChevronDown,
  Database,
  Download,
  History,
  Loader2,
  Play,
} from "lucide-react";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import { ErrorState } from "../components/common/ErrorState";
import { Skeleton } from "../components/common/Skeleton";
import { RowsTable } from "../components/copilot/RowsTable";
import { ApiError, NetworkError, TimeoutError } from "../api/client";
import {
  analyze,
  listSchema,
  runSql,
  type ResultSet,
  type SchemaTable,
} from "../api/sqlLab";
import type { ChartSpec } from "./computeChartSpecData";
import { buildBrowseStatement } from "./buildBrowseStatement";
import { toCsv } from "./toCsv";
import { pushHistory, readHistory } from "./queryHistory";
import {
  initialSqlLabViewState,
  sqlLabReducer,
} from "./sqlLabReducer";
import styles from "./SqlLabPage.module.css";

// Lazy-load the auto-dashboard so its heavy recharts dependency is only pulled
// in after the operator clicks "Analyze", keeping the initial SQL Lab page
// chunk small. Eager in tests to avoid jsdom dynamic-import flakiness (the same
// convention App.tsx uses for its route pages).
const isTest = import.meta.env.MODE === "test";
const AutoDashboard = isTest
  ? (await import("./AutoDashboard")).default
  : lazy(() => import("./AutoDashboard"));

// ---------------------------------------------------------------------------
// CSV export helper
// ---------------------------------------------------------------------------

/**
 * Trigger a client-side download of `csv` as `filename` via a Blob + anchor.
 *
 * Kept out of the pure `toCsv` module (which is DOM-free) so the serialisation
 * stays unit-testable; this thin wrapper owns the browser-only download step.
 */
function downloadCsv(csv: string, filename: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

/**
 * Translate a thrown error into the operator-facing message rendered in the
 * ErrorState.
 *
 * - 401/403 → the access-restricted notice (R6.5).
 * - Other `ApiError` (guard rejection 400, db error 400, timeout 504) → the
 *   backend-supplied detail so the operator sees the real reason (R6.3, R6.4).
 * - Timeout / network / unknown → a friendly fallback.
 */
function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) {
      return "SQL Lab access is restricted to operators.";
    }
    return error.detail;
  }
  if (error instanceof TimeoutError) {
    return "The query timed out. Try again or narrow the query.";
  }
  if (error instanceof NetworkError) {
    return "Network error. Check your connection and try again.";
  }
  return "Something went wrong while running the query.";
}

// ---------------------------------------------------------------------------
// Schema sidebar state + error mapping
// ---------------------------------------------------------------------------

/** Discriminated state for the schema sidebar (R7.5–R7.7). */
type SchemaState =
  | { kind: "loading" }
  | { kind: "loaded"; tables: SchemaTable[] }
  | { kind: "error"; message: string };

/**
 * Translate a thrown schema-listing error into the operator-facing indication
 * shown in the sidebar (R7.7).
 *
 * - 401/403 → the access-restricted notice.
 * - Other `ApiError` (e.g. the schema listing could not be retrieved) → the
 *   backend-supplied detail.
 * - Timeout / network / unknown → a friendly fallback.
 */
function describeSchemaError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) {
      return "SQL Lab access is restricted to operators.";
    }
    return error.detail;
  }
  if (error instanceof TimeoutError) {
    return "The schema listing timed out. Try reloading.";
  }
  if (error instanceof NetworkError) {
    return "Network error. Check your connection and try again.";
  }
  return "The schema listing could not be retrieved.";
}

// ---------------------------------------------------------------------------
// Auto-dashboard analysis state + error mapping
// ---------------------------------------------------------------------------

/**
 * Discriminated state for the AI auto-dashboard (Slice 4). The underlying
 * Result_Set rows stay visible in every state (R10.7, R10.8); this only drives
 * the dashboard region rendered below the results table.
 */
type AnalysisState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; spec: ChartSpec; resultSet: ResultSet }
  | { kind: "error"; message: string };

/**
 * Translate a thrown analysis error into the operator-facing message shown in
 * the dashboard error state (R10.7).
 *
 * - 401/403 → the access-restricted notice.
 * - Other `ApiError` (invalid Chart_Spec 400, model unavailable 503) → the
 *   backend-supplied detail.
 * - Timeout / network / unknown → a friendly fallback.
 */
function describeAnalysisError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) {
      return "SQL Lab access is restricted to operators.";
    }
    return error.detail;
  }
  if (error instanceof TimeoutError) {
    return "Analysis timed out. The result set may be too large to summarize.";
  }
  if (error instanceof NetworkError) {
    return "Network error. Check your connection and try again.";
  }
  return "The dashboard could not be generated.";
}

// ---------------------------------------------------------------------------
// SqlLabPage
// ---------------------------------------------------------------------------

export default function SqlLabPage() {
  const [sql, setSql] = useState("");
  const [viewState, dispatch] = useReducer(sqlLabReducer, initialSqlLabViewState);
  // Polite announcement text for assistive technology (R5.9).
  const [announcement, setAnnouncement] = useState("");

  // Which of the collapsible left-side panels are expanded. Both start
  // collapsed so the operator opts in by clicking the corresponding button;
  // neither Schema nor Query history is permanently displayed.
  const [openPanels, setOpenPanels] = useState<{
    schema: boolean;
    history: boolean;
  }>({ schema: false, history: false });

  function togglePanel(panel: "schema" | "history") {
    setOpenPanels((prev) => ({ ...prev, [panel]: !prev[panel] }));
  }

  const abortRef = useRef<AbortController | null>(null);
  const analysisAbortRef = useRef<AbortController | null>(null);

  // Auto-dashboard analysis state (Slice 4, R10.1/R10.7/R10.8). Kept separate
  // from the query view-state so the underlying rows always stay visible.
  const [analysisState, setAnalysisState] = useState<AnalysisState>({
    kind: "idle",
  });

  // Schema sidebar state (Slice 2, R7.5–R7.7). A small discriminated union so
  // exactly one of loading / loaded / error renders at a time. On error we do
  // NOT render a table list (R7.7).
  const [schemaState, setSchemaState] = useState<SchemaState>({ kind: "loading" });

  // Query history (Slice 5, R11.3–R11.5). Loaded from localStorage on mount via
  // the pure `readHistory` (never throws). `pushHistory` on a successful run
  // returns the updated newest-first list; a persist failure surfaces a notice
  // without discarding the Result_Set (R11.4).
  const [history, setHistory] = useState<string[]>(() => readHistory());
  const [historyNotice, setHistoryNotice] = useState("");

  // Notice shown when an export is blocked because there is no data to export
  // (R11.2). Cleared on a successful export.
  const [exportNotice, setExportNotice] = useState("");

  // Load the schema once on mount. A failed request shows an error indication
  // and never a partial/table list (R7.7); zero tables shows the empty
  // indication (R7.6).
  useEffect(() => {
    const controller = new AbortController();
    listSchema(controller.signal)
      .then((tables) => setSchemaState({ kind: "loaded", tables }))
      .catch((err) => {
        // Ignore an aborted request triggered by unmount.
        if (
          controller.signal.aborted &&
          err instanceof DOMException &&
          err.name === "AbortError"
        ) {
          return;
        }
        setSchemaState({ kind: "error", message: describeSchemaError(err) });
      });
    return () => controller.abort();
  }, []);

  const isLoading = viewState.kind === "loading";
  const isBlank = sql.trim().length === 0;
  // Run is disabled while a request is in flight or the editor is
  // empty/whitespace (R5.7 + task 7.4).
  const runDisabled = isLoading || isBlank;

  async function handleRun() {
    const trimmed = sql.trim();
    // Guard against activation with an empty/whitespace editor: never send a
    // request (R5.4). The persistent hint below already tells the operator a
    // non-empty query is required.
    if (trimmed.length === 0 || isLoading) return;

    // Cancel any prior in-flight request before starting a new one.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    // A new query invalidates any prior auto-dashboard; cancel and reset it.
    analysisAbortRef.current?.abort();
    setAnalysisState({ kind: "idle" });

    // Immediately enter the loading state so the Skeleton replaces any prior
    // result within 200 ms of submission (R6.1). The submitted SQL stays in the
    // editor so it is retained on error (R6.3, R6.4).
    dispatch({ type: "run" });
    setAnnouncement("Running query…");

    try {
      const result = await runSql(sql, controller.signal);
      dispatch({ type: "success", result });
      setAnnouncement(
        result.rowCount === 0
          ? "Query succeeded and returned no rows."
          : `Query succeeded and returned ${result.rowCount} ${
              result.rowCount === 1 ? "row" : "rows"
            }.`,
      );
      // Persist the submitted SQL to query history (R11.3). A localStorage
      // failure (e.g. quota) must NOT discard the Result_Set: complete the run
      // normally and surface a notice instead (R11.4).
      try {
        setHistory(pushHistory(sql));
        setHistoryNotice("");
      } catch {
        setHistoryNotice("Query history could not be saved.");
      }
    } catch (err) {
      // An aborted request was superseded by a newer run; ignore it.
      if (
        controller.signal.aborted &&
        err instanceof DOMException &&
        err.name === "AbortError"
      ) {
        return;
      }
      const message = describeError(err);
      dispatch({ type: "error", message });
      setAnnouncement(`Query failed: ${message}`);
    }
  }

  // Request an AI auto-dashboard for the current result set (Slice 4). The
  // underlying rows remain visible throughout; on failure a designed error
  // state is shown while the rows stay put (R10.7).
  async function handleAnalyze(resultSet: ResultSet) {
    analysisAbortRef.current?.abort();
    const controller = new AbortController();
    analysisAbortRef.current = controller;

    setAnalysisState({ kind: "loading" });
    try {
      const spec = await analyze(resultSet, "default", controller.signal);
      setAnalysisState({ kind: "ready", spec, resultSet });
    } catch (err) {
      if (
        controller.signal.aborted &&
        err instanceof DOMException &&
        err.name === "AbortError"
      ) {
        return;
      }
      setAnalysisState({ kind: "error", message: describeAnalysisError(err) });
    }
  }

  // Replace the entire editor contents with the canonical browse statement for
  // the selected table (R7.8).
  function handleSelectTable(tableName: string) {
    setSql(buildBrowseStatement(tableName));
  }

  // The Result_Set currently on screen, if any. Present for both a non-empty
  // result and a zero-row (empty) result; absent while idle/loading/error.
  const currentResult: ResultSet | null =
    viewState.kind === "result" || viewState.kind === "empty"
      ? viewState.result
      : null;

  // Export the current Result_Set to a downloaded CSV (R11.1). When there is no
  // Result_Set or it has zero rows, block the export and show a notice, leaving
  // the current view unchanged (R11.2).
  function handleExport() {
    if (!currentResult || currentResult.rowCount === 0) {
      setExportNotice("There is no data to export.");
      return;
    }
    setExportNotice("");
    downloadCsv(toCsv(currentResult), "sql-lab-export.csv");
  }

  // Replace the entire editor contents with a stored history statement (R11.5).
  function handleSelectHistory(entry: string) {
    setSql(entry);
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Ctrl/Cmd+Enter runs the query from the editor for keyboard operability.
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      void handleRun();
    }
  }

  return (
    <div className={styles.layout}>
      <PageHeader
        title="SQL Lab"
        subtitle="Run read-only SQL against operational data and view results in a table."
      />

      {/* Polite live region: announces begin/success/failure (R5.9). */}
      <div className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </div>

      <div className={styles.body}>
        {/* ── Left rail: selectable, expandable panels ── The Schema and Query
            history panels are no longer permanently displayed; each is a
            button that expands its content only after the operator clicks it. */}
        <aside className={styles.sidebar} aria-label="SQL Lab tools">
          {/* ── Schema panel (R7.5–R7.7) ── */}
          <div className={styles.panel}>
            <button
              type="button"
              className={styles.panelToggle}
              onClick={() => togglePanel("schema")}
              aria-expanded={openPanels.schema}
              aria-controls="sql-lab-schema-panel"
            >
              <Database size={14} aria-hidden="true" />
              <span className={styles.panelTitle}>Schema</span>
              <ChevronDown
                size={16}
                aria-hidden="true"
                className={
                  openPanels.schema ? styles.chevronOpen : styles.chevron
                }
              />
            </button>

            {openPanels.schema && (
              <div
                id="sql-lab-schema-panel"
                className={styles.panelBody}
              >
                {schemaState.kind === "loading" && (
                  <div
                    className={styles.sidebarSkeleton}
                    data-testid="sql-lab-schema-skeleton"
                    aria-hidden="true"
                  >
                    <Skeleton height={16} width="70%" />
                    <Skeleton height={14} width="90%" />
                    <Skeleton height={14} width="60%" />
                  </div>
                )}

                {schemaState.kind === "error" && (
                  <p className={styles.sidebarError} role="status">
                    <AlertTriangle size={14} aria-hidden="true" />
                    {schemaState.message}
                  </p>
                )}

                {schemaState.kind === "loaded" &&
                  (schemaState.tables.length === 0 ? (
                    <p className={styles.sidebarEmpty}>
                      No tables are available.
                    </p>
                  ) : (
                    <ul className={styles.tableList}>
                      {schemaState.tables.map((table) => (
                        <li key={table.name} className={styles.tableItem}>
                          {/* Selecting a table replaces the editor with its
                              canonical browse statement (R7.8). A real button
                              keeps the control focusable and keyboard-operable. */}
                          <button
                            type="button"
                            className={styles.tableName}
                            onClick={() => handleSelectTable(table.name)}
                          >
                            {table.name}
                          </button>
                          <ul className={styles.columnList}>
                            {table.columns.map((column) => (
                              <li
                                key={column.name}
                                className={styles.columnItem}
                              >
                                <span className={styles.columnName}>
                                  {column.name}
                                </span>
                                <span className={styles.columnType}>
                                  {column.type}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </li>
                      ))}
                    </ul>
                  ))}
              </div>
            )}
          </div>

          {/* ── Query history panel (R11.3–R11.5) ── Selecting an entry
              replaces the entire editor content with the stored SQL (R11.5). */}
          <div className={styles.panel}>
            <button
              type="button"
              className={styles.panelToggle}
              onClick={() => togglePanel("history")}
              aria-expanded={openPanels.history}
              aria-controls="sql-lab-history-panel"
            >
              <History size={14} aria-hidden="true" />
              <span className={styles.panelTitle}>Query history</span>
              <ChevronDown
                size={16}
                aria-hidden="true"
                className={
                  openPanels.history ? styles.chevronOpen : styles.chevron
                }
              />
            </button>

            {openPanels.history && (
              <div
                id="sql-lab-history-panel"
                className={styles.panelBody}
              >
                {history.length === 0 ? (
                  <p className={styles.sidebarEmpty}>No queries yet.</p>
                ) : (
                  <ul className={styles.historyList}>
                    {history.map((entry, index) => (
                      <li key={`${index}-${entry}`} className={styles.historyItem}>
                        <button
                          type="button"
                          className={styles.historyButton}
                          onClick={() => handleSelectHistory(entry)}
                          title={entry}
                        >
                          {entry}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </aside>

        {/* ── Main column: editor + results ── */}
        <div className={styles.main}>
          {/* ── Editor ── */}
          <div className={styles.editor}>
            <div className={styles.editorField}>
              <label htmlFor="sql-editor" className={styles.editorLabel}>
                SQL query
              </label>
              <textarea
                id="sql-editor"
                className={styles.textarea}
                value={sql}
                onChange={(e) => setSql(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="SELECT * FROM …"
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                aria-describedby="sql-editor-hint"
              />
            </div>

            <div className={styles.actions}>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleRun()}
                disabled={runDisabled}
              >
                {isLoading ? (
                  <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
                ) : (
                  <Play size={14} aria-hidden="true" />
                )}
                {isLoading ? "Running…" : "Run"}
              </button>
              {/* Export the current Result_Set as CSV (R11.1). Always available
                  so a request with no/zero rows can be blocked with a notice
                  (R11.2). */}
              <button
                type="button"
                className="btn"
                onClick={handleExport}
                data-testid="sql-lab-export"
              >
                <Download size={14} aria-hidden="true" />
                Export CSV
              </button>
              <span
                id="sql-editor-hint"
                className={isBlank ? styles.hintWarn : styles.hint}
              >
                {isBlank
                  ? "Enter a non-empty query to run."
                  : "Press Ctrl+Enter to run."}
              </span>
            </div>

            {/* Export-blocked notice (R11.2). The current view is left
                unchanged; this only reports that there is no data to export. */}
            {exportNotice && (
              <p className={styles.notice} role="status">
                <AlertTriangle size={14} aria-hidden="true" />
                {exportNotice}
              </p>
            )}

            {/* Query-history persist-failure notice (R11.4). The run completed
                and the Result_Set is retained; only saving history failed. */}
            {historyNotice && (
              <p className={styles.notice} role="status">
                <AlertTriangle size={14} aria-hidden="true" />
                {historyNotice}
              </p>
            )}
          </div>

          {/* ── Results (exactly one state at a time, R6.6) ── */}
          {viewState.kind === "loading" && (
            <div
              className={styles.skeletonBlock}
              data-testid="sql-lab-skeleton"
              aria-hidden="true"
            >
              <Skeleton height={20} width="40%" />
              <Skeleton height={16} />
              <Skeleton height={16} />
              <Skeleton height={16} width="80%" />
            </div>
          )}

          {viewState.kind === "empty" && (
            <EmptyState
              icon={Database}
              title="No rows returned"
              body="The query succeeded but returned no rows."
            />
          )}

          {viewState.kind === "result" && (
            <section className={styles.results} aria-label="Query results">
              {viewState.result.truncated && (
                <p className={styles.truncationBanner} role="status">
                  <AlertTriangle size={16} aria-hidden="true" />
                  Results were limited to the first {viewState.result.rowCount} rows.
                </p>
              )}
              <div className={styles.resultMeta}>
                <span>
                  Rows: <strong>{viewState.result.rowCount}</strong>
                </span>
                <span>
                  Duration: <strong>{viewState.result.durationMs} ms</strong>
                </span>
              </div>
              <RowsTable rows={viewState.result.rows} />

              {/* ── Auto-dashboard (Slice 4) ── The Analyze affordance appears
                  only when a result set is present. The rows above stay visible
                  in every analysis state (R10.7, R10.8). */}
              <div className={styles.analyzeBar}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => void handleAnalyze(viewState.result)}
                  disabled={analysisState.kind === "loading"}
                >
                  {analysisState.kind === "loading" ? (
                    <Loader2
                      size={14}
                      className={styles.spinner}
                      aria-hidden="true"
                    />
                  ) : (
                    <BarChart3 size={14} aria-hidden="true" />
                  )}
                  {analysisState.kind === "loading"
                    ? "Analyzing…"
                    : "Analyze"}
                </button>
              </div>

              {analysisState.kind === "loading" && (
                <div
                  className={styles.skeletonBlock}
                  data-testid="sql-lab-analysis-skeleton"
                  aria-hidden="true"
                >
                  <Skeleton height={20} width="30%" />
                  <Skeleton height={80} />
                </div>
              )}

              {analysisState.kind === "error" && (
                <ErrorState
                  title="Dashboard unavailable"
                  body={analysisState.message}
                />
              )}

              {analysisState.kind === "ready" && (
                <Suspense
                  fallback={
                    <div
                      className={styles.skeletonBlock}
                      data-testid="sql-lab-analysis-skeleton"
                      aria-hidden="true"
                    >
                      <Skeleton height={20} width="30%" />
                      <Skeleton height={80} />
                    </div>
                  }
                >
                  <AutoDashboard
                    spec={analysisState.spec}
                    resultSet={analysisState.resultSet}
                  />
                </Suspense>
              )}
            </section>
          )}

          {viewState.kind === "error" && (
            <ErrorState title="Query failed" body={viewState.message} />
          )}
        </div>
      </div>
    </div>
  );
}
