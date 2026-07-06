# Implementation Plan: SQL Lab (Data Explorer)

## Overview

This plan converts the SQL Lab design into incremental coding steps, ordered by
the vertical slices defined in the design. Slice 1 (Requirements 1–6) is the
shippable MVP: viewer-role config, the SQL guard, the read-only executor, the
`POST /sql/run` route, the query UI, and honest loading/empty/error states.
Later slices add the schema sidebar (Slice 2), audit logging (Slice 3), the AI
auto-dashboard (Slice 4), and optional convenience features (Slice 5).

Backend code lives under `src/rag_system/` (Python, psycopg + FastAPI +
pydantic-settings; property tests with Hypothesis + pytest). Frontend code lives
under `frontendkimchi/src/` (TypeScript + React; property tests with fast-check +
Vitest). Each step builds on the previous ones and ends by wiring the new
component into the running system.

## Tasks

- [x] 1. SQL Lab Settings and configuration
  - [x] 1.1 Add SQL Lab fields and validators to `Settings`
    - Add `sql_viewer_db_user`, `sql_viewer_db_password`, `sql_lab_row_limit`, `sql_lab_statement_timeout_ms`, and `sql_lab_sensitive_tables` fields to `rag_system/config.py` following the existing alias + `field_validator` convention
    - Add the auto-dashboard analysis model-id fields `sql_lab_analysis_model_id` (default `gemini-3.5-flash`, alias `SQL_LAB_ANALYSIS_MODEL_ID`) and `sql_lab_deep_analysis_model_id` (default `gemini-3.1-pro`, alias `SQL_LAB_DEEP_ANALYSIS_MODEL_ID`) so all SQL Lab config, including Slice 4 model selection, lives in one place
    - Add range validators for row limit ([1, 10000]) and statement timeout ([1, 60000]) that fail startup naming the offending key
    - Add `require_sql_viewer_credentials()` returning a keyed, value-free `SqlLabConfigError` when a credential is missing
    - Reuse `copilot_db_host/port/name/sslmode` for the connection endpoint; read all config through `Settings` only
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.7, 1.8, 1.9, 9.8, 9.9_
  - [x] 1.2 Write property test for config bounds
    - **Property 6: Row_Limit and Statement_Timeout config bounds are enforced at startup**
    - **Validates: Requirements 1.7, 1.8, 1.9**
  - [x] 1.3 Write property test for missing/failed viewer credentials
    - **Property 7: Missing or failed viewer credentials produce a keyed, secret-free error**
    - **Validates: Requirements 1.5, 1.6**
  - [x]* 1.4 Write unit tests for env-var wiring and connection assembly
    - Verify new Settings fields read from their env aliases and the viewer connection reuses `COPILOT_DB_*` endpoint values
    - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. SQL guard (secondary guardrail)
  - [x] 2.1 Implement `SqlLabGuard` in `rag_system/sql_lab/guard.py`
    - Reuse `rag_system.copilot._strip_sql_comments` for string-literal-aware comment removal
    - Fail-closed pipeline: strip comments, reject empty/whitespace, parse with `sqlglot`, reject on parse failure, reject if more than one statement (ignoring one optional trailing `;`), reject non-`Select` roots, reject any write/DDL/administrative node, reject `WITH` in v1, reject denylisted sensitive-table references; allow `SELECT` and `SELECT *`
    - Raise `SqlLabValidationError` whose message names the specific rejection reason
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 2.4_
  - [x] 2.2 Write property test for comment stripping
    - **Property 1: Comment stripping preserves string literals**
    - **Validates: Requirements 3.1**
  - [x] 2.3 Write property test for allowed single SELECT
    - **Property 2: A single read-only SELECT (including `SELECT *`) is allowed**
    - **Validates: Requirements 3.2, 3.3**
  - [x] 2.4 Write property test for rejected statements
    - **Property 3: Every non-(single read-only SELECT) input is rejected with a reason**
    - **Validates: Requirements 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**
  - [x] 2.5 Write property test for sensitive-table denylist rejection
    - **Property 5: Denylisted sensitive-table references are rejected before execution**
    - **Validates: Requirements 2.4**

- [x] 3. SQL executor (read-only viewer connection)
  - [x] 3.1 Implement `SqlLabExecutor` in `rag_system/sql_lab/executor.py`
    - Mirror the `PostgresCopilotExecutor` transaction body verbatim: `SET TRANSACTION READ ONLY`, `set_config('statement_timeout', <ms>, true)`, `fetchmany(row_limit + 1)`, `rollback`
    - Authenticate with `SQL_VIEWER_DB_USER`/`SQL_VIEWER_DB_PASSWORD` over the `COPILOT_DB_*` endpoint; measure `duration_ms` with `time.perf_counter`
    - Raise `SqlLabConfigError`, `SqlLabConnectionError`, `SqlLabTimeoutError`, and `SqlLabExecutionError` (carrying the db message) as designed
    - _Requirements: 4.4, 1.3, 1.5, 1.6_
  - [x]* 3.2 Write unit test for transaction sequence via spy connection
    - Assert `SET TRANSACTION READ ONLY` → `set_config('statement_timeout', ms, true)` → `fetchmany` → `rollback` in order, with rollback always run
    - _Requirements: 4.4_
  - [x]* 3.3 Write unit tests for timeout and generic error paths
    - Assert rollback + `SqlLabTimeoutError` on timeout and rollback + `SqlLabExecutionError` on other db errors, using a mock driver
    - _Requirements: 4.9, 4.13_

- [x] 4. SQL Lab service and Result_Set shaping
  - [x] 4.1 Implement `SqlLabService` in `rag_system/sql_lab/service.py`
    - Orchestrate guard → execute and shape the `Result_Set` (`columns`, `rows`, `rowCount`, `durationMs`, `sql`, `truncated`)
    - Trim `Row_Limit + 1` fetched rows to `Row_Limit`, set `truncated` accordingly, echo the submitted `sql`
    - Ensure a guard-rejected statement never reaches the executor
    - _Requirements: 4.5, 4.6, 4.7, 4.8, 4.10, 3.10, 4.11, 4.12_
  - [x] 4.2 Write property test for Result_Set shaping
    - **Property 8: Result_Set shaping enforces the row limit and truncation flag**
    - **Validates: Requirements 4.5, 4.6, 4.7, 4.8, 4.10**
  - [x] 4.3 Write property test for rejected statements never executing
    - **Property 4: A rejected statement is never executed**
    - **Validates: Requirements 3.10, 4.11, 4.12**

- [x] 5. Query execution route
  - [x] 5.1 Implement `POST /sql/run` in `rag_system/sql_lab/router.py` and mount it
    - Define the `SqlRunRequest` model (1–10000 chars, non-whitespace required) and wire `require_operator`
    - Map errors: validation/guard → `400`, missing config/connection → `400` (keyed, value-free), timeout → `504`, other db error → `400` with db message
    - Register the router via `app.include_router`
    - _Requirements: 4.1, 4.2, 4.3, 4.9, 4.11, 4.12, 4.13_
  - [x]* 5.2 Write unit tests for route auth, length boundary, and error mapping
    - Cover missing JWT (401), non-operator (403), SQL length at 10000/10001, empty/whitespace, and each error-status mapping
    - _Requirements: 4.1, 4.2, 4.3, 4.9, 4.12, 4.13_

- [x] 6. Checkpoint - Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Frontend query UI (tab, editor, results, honest states)
  - [x] 7.1 Add typed API module `frontendkimchi/src/api/sqlLab.ts`
    - Implement `runSql(sql, signal?)` returning `ResultSet` through `apiClient` with `TIMEOUT_LONG_MS`; declare the shared types (`ResultSet`, `RunSqlRequest`)
    - _Requirements: 5.3_
  - [x] 7.2 Implement the `sqlLabReducer` view-state machine
    - Extract a pure reducer producing exactly one of `idle | loading | empty | result | error` per lifecycle event
    - _Requirements: 6.6_
  - [x]* 7.3 Write property test for the view-state reducer
    - **Property 9: The SqlLabPage renders exactly one state at a time**
    - **Validates: Requirements 6.6**
  - [x] 7.4 Implement `SqlLabPage.tsx`
    - Render a labeled multi-line editor, keyboard-operable Run control (disabled while in-flight or when empty/whitespace), and drive Skeleton/EmptyState/RowsTable/ErrorState from the reducer
    - Show Skeleton within 200 ms hiding any prior result, a persistent truncation banner, retain submitted SQL on error, and announce begin/success/failure via a polite `aria-live` region
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 6.1, 6.2, 6.3, 6.4, 6.5_
  - [x] 7.5 Register the SQL Lab tab and route
    - Insert the operator-gated `{ to: "/sql-lab", label: "SQL Lab", Icon: Database, operatorOnly: true }` entry into `PrimaryNav` `TABS` immediately after "Documents" and before "Evaluation" (Copilot → AI Observability → Documents → **SQL Lab** → Evaluation → Feedback → Replay Lab → Knowledge Gaps), making it the first operator-only tab and the fourth tab overall; register the lazy route in `App.tsx`
    - _Requirements: 5.1_
  - [x]* 7.6 Write component tests for page states and accessibility
    - Cover Skeleton-within-200ms, EmptyState vs ErrorState, guard/db/auth error rendering with SQL retention, Run-disabled-in-flight, truncation banner, aria-live announcements, and an axe smoke check
    - _Requirements: 5.1, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 8. Viewer role provisioning
  - [x] 8.1 Create the viewer-role provisioning SQL script and documentation
    - Add a checked-in SQL script and docs covering `CREATE ROLE`, `REVOKE ALL`, and `SELECT` grants only on approved tables, with `users`/`refresh_tokens` explicitly excluded
    - _Requirements: 2.7, 2.3_
  - [x] 8.2 Write integration tests for role scoping against Postgres
    - Verify the viewer role is denied `users`/`refresh_tokens` (authorization error, zero rows), rejects writes/DDL at the db level, and returns rows for approved tables
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_

- [x] 9. Checkpoint - Slice 1 (MVP) complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Schema sidebar and quick browse (Slice 2)
  - [x] 10.1 Implement the `GET /sql/schema` endpoint
    - List tables and columns from `information_schema`, filtered to objects the viewer role holds a `SELECT` grant on (via `role_table_grants`/`table_privileges`); reuse `require_operator`; error on failure without partial lists
    - _Requirements: 7.1, 7.2, 7.3, 7.4_
  - [x]* 10.2 Write property test for the schema-listing filter
    - **Property 10: Schema listing excludes ungranted and sensitive tables**
    - **Validates: Requirements 7.3**
  - [x]* 10.3 Write integration test for the schema endpoint
    - Verify only granted tables are returned and sensitive tables never appear
    - _Requirements: 7.1_
  - [x] 10.4 Add `listSchema` API and render the sidebar
    - Add `listSchema` to `api/sqlLab.ts` and render tables/columns in a sidebar with empty and error indications on `SqlLabPage`
    - _Requirements: 7.5, 7.6, 7.7_
  - [x] 10.5 Implement `buildBrowseStatement` and table selection
    - Extract a pure function producing `SELECT * FROM <table> LIMIT 100`; on table selection replace the editor contents with it
    - _Requirements: 7.8_
  - [x]* 10.6 Write property test for the browse statement builder
    - **Property 11: Table selection produces the canonical browse statement**
    - **Validates: Requirements 7.8**
  - [x]* 10.7 Write component tests for the sidebar
    - Cover render, empty, and error states of the sidebar
    - _Requirements: 7.5, 7.6, 7.7_

- [x] 11. Audit logging of executions (Slice 3)
  - [x] 11.1 Implement `SqlLabAuditStore` and the audit record
    - Follow the `PostgresLogStore`/`PostgresTraceStore` style using the copilot (write-capable) role; define the audit table/migration and record construction (user identity, SQL truncated to 10000 chars, UTC timestamp, duration, row count, outcome)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - [x] 11.2 Wire audit recording into `SqlLabService`
    - Persist exactly one record per outcome (success / post-guard error / guard rejection); on persist failure return an error and withhold result rows
    - _Requirements: 8.1, 8.2, 8.3, 8.6_
  - [x]* 11.3 Write property test for audit records
    - **Property 12: Each request produces exactly one well-formed audit record**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**
  - [x]* 11.4 Write unit test for audit-persist failure
    - Verify a persist failure yields an error response and no result rows
    - _Requirements: 8.6_

- [x] 12. AI auto-dashboard analysis endpoint (Slice 4)
  - [x] 12.1 Implement the `ChartSpec` schema and validator
    - Define the strict declarative aggregation schema: KPIs and 1–3 charts expressed only as references to column names plus operations drawn from the bounded allowed set (`sum`, `count`, `avg`, `min`, `max`, group-by via `xColumn`), an optional insight ≤ 200 chars, **no precomputed numeric values anywhere**, and extra fields / HTML / JavaScript / executable content rejected; add a validation function that returns an error on failure
    - _Requirements: 9.4, 9.5, 9.6, 9.7, 10.2_
  - [x]* 12.2 Write property test for Chart_Spec validation
    - **Property 14: Chart_Spec schema validation rejects invalid or non-declarative content**
    - **Validates: Requirements 9.5, 9.6, 9.7, 10.2**
  - [x] 12.3 Implement the dedicated Gemini structured-output client
    - Construct a `google-genai` `Client` (Vertex AI) with `HttpOptions(timeout=60_000)`; expose a call that runs `generate_content` with a `GenerateContentConfig` carrying `response_mime_type="application/json"` and `response_schema=CHART_SPEC_RESPONSE_SCHEMA`
    - Select the model id per analysis mode from the new Settings fields: `sql_lab_analysis_model_id` (default `gemini-3.5-flash`) for default mode and `sql_lab_deep_analysis_model_id` (default `gemini-3.1-pro`) for deep mode
    - This exists because the shared `TextLLM.generate` (`rag_system/llm.py`) exposes neither `response_schema`/`response_mime_type` nor a per-call model override
    - _Requirements: 9.4, 9.8, 9.9, 9.10_
  - [x] 12.4 Implement `ChartSpecAnalyzer`
    - Build the compact payload (column names, inferred types, row count, ≤ 20-row sample); request structured output through the dedicated Gemini structured-output client from task 12.3 (default → Flash, deep → Pro); the 60s budget is enforced at the client's `HttpOptions` level; validate output against the schema before returning
    - _Requirements: 9.2, 9.3, 9.8, 9.9, 9.10_
  - [x]* 12.5 Write property test for the analysis payload
    - **Property 13: The analysis payload sends only a bounded sample**
    - **Validates: Requirements 9.2, 9.3**
  - [x] 12.6 Implement the `POST /sql/analyze` route
    - Add the operator-gated route returning a validated `Chart_Spec`; map invalid-spec/LLM-unavailable errors and leave the source Result_Set unchanged
    - _Requirements: 9.1, 9.6, 9.10_
  - [x]* 12.7 Write unit tests for the analyze route
    - Cover auth (401/403), default vs deep model selection, and LLM unavailable/slow handling
    - _Requirements: 9.1, 9.8, 9.9, 9.10_

- [x] 13. Auto-dashboard rendering with locally computed numbers (Slice 4)
  - [x] 13.1 Implement `computeChartSpecData`
    - Extract a pure helper that validates every referenced column (`kpis[].column`, `charts[].xColumn`, `charts[].series[].column`) exists in `ResultSet.columns` and every `op` is in the allowed set (`sum`, `count`, `avg`, `min`, `max`, group-by via `xColumn`)
    - Compute each KPI value and chart series value locally from the actual `ResultSet.rows` using only the declared operation over the referenced column (grouping by `xColumn` for charts)
    - Omit and mark uncomputable any KPI or chart that references an unknown column or a disallowed op
    - _Requirements: 10.3, 10.4_
  - [x]* 13.2 Write property test for local computation
    - **Property 15: The dashboard computes every displayed value locally from the rows**
    - **Validates: Requirements 10.3, 10.4**
  - [x] 13.3 Implement the bounded insight helper
    - Extract a pure function returning at most one insight line of at most 200 characters
    - _Requirements: 10.5_
  - [x]* 13.4 Write property test for the insight bound
    - **Property 16: The rendered insight line is bounded**
    - **Validates: Requirements 10.5**
  - [x] 13.5 Implement the `AutoDashboard` component and wire analysis
    - Add `analyze` to `api/sqlLab.ts`, render KPI cards and 1–3 recharts charts from the locally computed aggregates (via `computeChartSpecData`) with keyboard-reachable associated data tables, omit/mark uncomputable KPIs/charts, and handle dashboard error/empty states while keeping the underlying rows visible
    - _Requirements: 10.1, 10.4, 10.5, 10.6, 10.7, 10.8_
  - [x]* 13.6 Write component tests for the dashboard
    - Cover KPI/chart rendering from locally computed aggregates, associated data tables (axe smoke), uncomputable KPI/chart omission and marking (R10.4), and dashboard error/empty states with rows retained (R10.7, R10.8)
    - _Requirements: 10.1, 10.4, 10.6, 10.7, 10.8_

- [x] 14. Checkpoint - Slices 2–4 complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Convenience features (Slice 5, optional/later)
  - [x] 15.1 Implement the `toCsv` export function
    - Extract a pure function emitting column names first, then displayed rows in order, with standard CSV quoting/escaping
    - _Requirements: 11.1_
  - [x]* 15.2 Write property test for CSV export
    - **Property 17: CSV export round-trips columns and rows in displayed order**
    - **Validates: Requirements 11.1**
  - [x] 15.3 Implement the `pushHistory` function
    - Extract a pure function persisting to localStorage newest-first, capped at 50 entries with oldest eviction
    - _Requirements: 11.3_
  - [x]* 15.4 Write property test for query-history eviction
    - **Property 18: Query history is bounded, newest-first, and evicts the oldest**
    - **Validates: Requirements 11.3**
  - [x] 15.5 Enable read-only CTE support in `SqlLabGuard`
    - Add the `allow_cte=True` path that recurses the parsed tree, allowing read-only `WITH`/sub-selects and rejecting any data-modifying node at any depth with a descriptive message
    - _Requirements: 11.6, 11.7_
  - [x]* 15.6 Write property test for read-only CTE handling
    - **Property 19: Read-only CTEs are allowed and any nested data-modification is rejected**
    - **Validates: Requirements 11.6, 11.7**
  - [x] 15.7 Integrate CSV export and query history into `SqlLabPage`
    - Wire the export control (blocked with a message when no/zero rows), history persistence on execute (with a "history could not be saved" notice on failure keeping the Result_Set), and history-entry selection replacing the editor
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
  - [x]* 15.8 Write component tests for CSV export and history UI
    - Cover export-blocked-on-empty, the history persist-failure notice, and history-entry selection replacing the editor
    - _Requirements: 11.2, 11.4, 11.5_

- [x] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references specific requirement sub-clauses for traceability, and each property test names its property number and validated requirements.
- The implementation order follows the vertical slices: complete Slice 1 (tasks 1–9) before later slices.
- The dedicated read-only Postgres role is the primary security boundary (tasks 8, verified by integration tests); the SQL guard is the secondary guardrail (task 2, verified by property tests).
- Checkpoints ensure incremental validation at slice boundaries.

## Task Dependency Graph

The waves below respect the vertical slicing: every Slice 1 task (waves 0–4)
completes before any Slice 2 task (waves 5–8), which completes before Slice 3
(waves 9–11), then Slice 4 (waves 12–17), then Slice 5 (waves 18–20). No
higher-slice task is scheduled into a wave that precedes the completion of a
lower slice. Within each slice, independent tasks are parallelized, same-file
writers are placed in separate waves, and tests follow the code they cover.

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "7.1", "7.2", "8.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.2", "2.3", "2.4", "2.5", "3.1", "7.3", "7.4", "8.2"] },
    { "id": 2, "tasks": ["3.2", "3.3", "4.1", "7.5"] },
    { "id": 3, "tasks": ["4.2", "4.3", "5.1", "7.6"] },
    { "id": 4, "tasks": ["5.2"] },
    { "id": 5, "tasks": ["10.1"] },
    { "id": 6, "tasks": ["10.2", "10.3", "10.4"] },
    { "id": 7, "tasks": ["10.5", "10.7"] },
    { "id": 8, "tasks": ["10.6"] },
    { "id": 9, "tasks": ["11.1"] },
    { "id": 10, "tasks": ["11.2", "11.3"] },
    { "id": 11, "tasks": ["11.4"] },
    { "id": 12, "tasks": ["12.1"] },
    { "id": 13, "tasks": ["12.2", "12.3", "13.1", "13.3"] },
    { "id": 14, "tasks": ["12.4", "13.2", "13.4"] },
    { "id": 15, "tasks": ["12.5", "12.6"] },
    { "id": 16, "tasks": ["12.7", "13.5"] },
    { "id": 17, "tasks": ["13.6"] },
    { "id": 18, "tasks": ["15.1", "15.3", "15.5"] },
    { "id": 19, "tasks": ["15.2", "15.4", "15.6", "15.7"] },
    { "id": 20, "tasks": ["15.8"] }
  ]
}
```
