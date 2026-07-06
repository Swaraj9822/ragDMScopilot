# Requirements Document

## Introduction

SQL Lab (Data Explorer) is a new operator-only, read-only tab in the RAG Console
that lets the internal team run ad-hoc read-only SQL and view results in a table
directly in the app, replacing the current workflow of connecting to pgAdmin. An
optional AI-generated analytics dashboard summarizes result sets without ever
fabricating numbers.

This specification is sliced **vertically**: each requirement is an
independently shippable increment that delivers backend + API + UI + tests
together. Slice 1 is the minimum viable product; each later slice builds on the
shipped slice before it. Requirements 1-6 belong to Slice 1 (MVP), Requirement 7
to Slice 2, Requirement 8 to Slice 3, Requirements 9-10 to Slice 4, and
Requirement 11 to Slice 5 (optional/later).

The overriding security stance: **the dedicated read-only Postgres role is the
primary security boundary; the SQL parser guard is a secondary guardrail, not the
lock.** One Postgres instance (the `COPILOT_DB_*` connection) holds auth data
(`users`, `refresh_tokens`), traces, logs, and business data, so the SQL Lab role
MUST be scoped so it cannot read sensitive tables.

The feature reuses the existing `COPILOT_DB_HOST/PORT/NAME/SSLMODE` connection
settings and the `PostgresCopilotExecutor` transaction pattern verbatim,
regardless of the underlying cloud provider (the repository's docs disagree on
GCP/Neon vs AWS/RDS; this specification does not resolve that and depends only on
the shared connection settings and transaction pattern).

## Glossary

- **SQL_Lab**: The overall operator-only read-only data-exploration feature (tab, backend routes, and read-only role) added to the RAG Console.
- **SQL_Lab_Backend**: The backend HTTP layer serving SQL Lab (the `POST /sql/run`, schema, and `POST /sql/analyze` routes and their handlers).
- **SQL_Guard**: The `sqlglot`-based SQL parser guardrail that decides whether a submitted statement is an allowed read-only query.
- **SQL_Executor**: The database execution component that runs approved SQL using the `PostgresCopilotExecutor` transaction pattern (`SET TRANSACTION READ ONLY`, transaction-local `statement_timeout`, `fetchmany`, `rollback`).
- **SQL_Viewer_Role**: The dedicated read-only Postgres role used exclusively by SQL Lab, authenticated with the `SQL_VIEWER_DB_USER` and `SQL_VIEWER_DB_PASSWORD` credentials over the shared `COPILOT_DB_HOST/PORT/NAME/SSLMODE` connection.
- **Settings**: The `rag_system.config.Settings` object (pydantic-settings), the only place environment variables are read.
- **Operator**: An authenticated user whose role is operator or admin, as determined by the existing JWT auth subsystem.
- **SqlLabPage**: The frontend React page (`frontendkimchi/src/pages/`) that renders the SQL Lab tab.
- **RowsTable**: The existing frontend component used to render tabular result rows.
- **PrimaryNav**: The existing frontend top-level navigation component listing the console tabs.
- **Chart_Spec**: A validated, schema-constrained JSON object describing KPI cards and charts for the AI auto-dashboard; it contains declarative aggregation instructions (references to column names present in the Result_Set plus a bounded set of allowed aggregation operations) rather than precomputed numeric values, and it contains no HTML, JavaScript, or executable code.
- **AutoDashboard**: The frontend component that renders a Chart_Spec using recharts.
- **Sensitive_Table**: Any table holding authentication or credential data, specifically `users` (bcrypt password hashes) and `refresh_tokens`.
- **Result_Set**: The `{columns, rows, rowCount, durationMs, sql, truncated}` object returned by `POST /sql/run`.
- **Row_Limit**: The maximum number of rows a single query may return, sourced from Settings.
- **Statement_Timeout**: The maximum execution time for a single query, sourced from Settings, applied transaction-locally.

## Requirements

### Requirement 1: Dedicated read-only viewer role and configuration (Slice 1)

**User Story:** As an operator, I want SQL Lab to connect through a dedicated read-only database role, so that ad-hoc queries can never modify data and can never read authentication secrets.

#### Acceptance Criteria

1. THE Settings SHALL expose a `sql_viewer_db_user` value read from the `SQL_VIEWER_DB_USER` environment variable.
2. THE Settings SHALL expose a `sql_viewer_db_password` value read from the `SQL_VIEWER_DB_PASSWORD` environment variable.
3. THE SQL_Executor SHALL connect using `SQL_VIEWER_DB_USER` and `SQL_VIEWER_DB_PASSWORD` for authentication while reusing the `COPILOT_DB_HOST`, `COPILOT_DB_PORT`, `COPILOT_DB_NAME`, and `COPILOT_DB_SSLMODE` values from Settings for the connection endpoint.
4. WHERE any SQL Lab configuration value is required, THE SQL_Lab_Backend SHALL read it exclusively through Settings rather than reading environment variables directly.
5. IF `SQL_VIEWER_DB_USER` or `SQL_VIEWER_DB_PASSWORD` is absent when a query execution is attempted, THEN THE SQL_Lab_Backend SHALL return an error response identifying the missing configuration by key name and SHALL NOT include the missing value.
6. IF the SQL_Executor cannot establish a connection using the viewer credentials when a query execution is attempted, THEN THE SQL_Lab_Backend SHALL return an error response indicating the viewer database connection failed and SHALL NOT include the credential values.
7. THE Settings SHALL expose a SQL Lab Row_Limit value with a default of 100 rows, constrained to an integer between 1 and 10000 inclusive.
8. THE Settings SHALL expose a SQL Lab Statement_Timeout value in milliseconds with a default of 10000 milliseconds, constrained to an integer between 1 and 60000 inclusive.
9. IF a SQL Lab Row_Limit or Statement_Timeout configuration value is non-integer or outside its allowed range, THEN THE Settings SHALL fail startup validation with an error identifying the offending configuration key.

### Requirement 2: Read-only role scoping prevents access to sensitive tables (Slice 1)

**User Story:** As a security-conscious operator, I want the SQL Lab role to be denied access to authentication tables, so that bcrypt password hashes and refresh tokens can never be read through SQL Lab.

#### Acceptance Criteria

1. WHEN a query that references the `users` table is executed directly against the database as the SQL_Viewer_Role, bypassing or in the absence of the SQL_Guard denylist, THE database SHALL deny access with an authorization error and SHALL return zero rows from the `users` table, because the SQL_Viewer_Role holds no `SELECT` privilege on that table. (Primary security boundary, verified at the database/provisioning level.)
2. WHEN a query that references the `refresh_tokens` table is executed directly against the database as the SQL_Viewer_Role, bypassing or in the absence of the SQL_Guard denylist, THE database SHALL deny access with an authorization error and SHALL return zero rows from the `refresh_tokens` table, because the SQL_Viewer_Role holds no `SELECT` privilege on that table. (Primary security boundary, verified at the database/provisioning level.)
3. THE SQL_Lab SHALL grant the SQL_Viewer_Role `SELECT` privileges only on explicitly approved, non-Sensitive_Table tables as the primary access-control mechanism, and the SQL_Viewer_Role SHALL hold no privilege on `users` or `refresh_tokens`.
4. WHERE a Sensitive_Table denylist is configured, IF a submitted query references a Sensitive_Table, THEN THE SQL_Guard SHALL reject the query before execution with a guard rejection error and SHALL return zero rows. (Secondary guardrail, verified at the API level before the query reaches the database.)
5. IF the SQL_Viewer_Role attempts any write, insert, update, delete, or schema-modifying statement that bypasses the SQL_Guard, THEN THE database SHALL reject the statement because the role holds no write privilege, and the database state SHALL remain unchanged.
6. WHEN the SQL_Viewer_Role reads an approved table for which it holds a `SELECT` grant, THE SQL_Lab_Backend SHALL return the matching rows without an authorization error.
7. THE SQL_Lab SHALL document the exact `SELECT` grant and role-creation steps required to provision the SQL_Viewer_Role, including the approved-table list and the excluded Sensitive_Table set.

### Requirement 3: SQL guard allows a single read-only SELECT and blocks everything else (Slice 1)

**User Story:** As an operator, I want a parser guard that permits a single read-only SELECT while rejecting unsafe statements, so that obvious mistakes and injection attempts are caught before reaching the database.

#### Acceptance Criteria

1. WHILE validating a submitted statement, THE SQL_Guard SHALL remove all line (`--`) and block (`/* */`) comments before classifying the statement, without treating comment markers that appear inside string literals as comments.
2. WHEN a submitted statement, after comment removal, parses as exactly one `SELECT` statement, THE SQL_Guard SHALL classify the statement as allowed.
3. WHEN a submitted statement, after comment removal, is a `SELECT *` (star projection), THE SQL_Guard SHALL classify the statement as allowed.
4. IF a submitted statement, after comment removal, contains any write or data-modifying operation (`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`), THEN THE SQL_Guard SHALL reject the statement with a message identifying the disallowed operation as the rejection reason.
5. IF a submitted statement, after comment removal, contains any DDL or administrative operation (`CREATE`, `ALTER`, `DROP`, `GRANT`, `REVOKE`, `COPY`, `SET`, `VACUUM`, or any other non-`SELECT` command), THEN THE SQL_Guard SHALL reject the statement with a message identifying the disallowed operation as the rejection reason.
6. IF a submitted statement, after comment removal and excluding a single optional trailing semicolon, contains more than one statement, THEN THE SQL_Guard SHALL reject the statement with a message identifying multiple statements as the rejection reason.
7. IF a submitted statement, after comment removal, contains a `WITH` clause (common table expression), THEN THE SQL_Guard SHALL reject the statement in version 1 with a message identifying the `WITH` clause as the rejection reason.
8. IF a submitted statement cannot be parsed by `sqlglot`, THEN THE SQL_Guard SHALL reject the statement with a message identifying the parse failure as the rejection reason and SHALL NOT execute the statement.
9. IF a submitted statement is empty or contains only whitespace after comment removal, THEN THE SQL_Guard SHALL reject the statement with a message identifying the empty input as the rejection reason.
10. WHEN the SQL_Guard rejects a statement, THE SQL_Lab_Backend SHALL NOT execute the statement against the database and SHALL leave database state unchanged.

### Requirement 4: Authenticated operator-only query execution endpoint (Slice 1)

**User Story:** As an operator, I want to POST read-only SQL to a protected endpoint and receive result rows, so that I can inspect backend data without leaving the app.

#### Acceptance Criteria

1. THE SQL_Lab_Backend SHALL expose a `POST /sql/run` route that accepts a SQL string of at most 10000 characters.
2. IF a request to `POST /sql/run` arrives without a valid JWT bearer token, THEN THE SQL_Lab_Backend SHALL reject the request with an unauthorized error and SHALL NOT execute the statement.
3. IF an authenticated request to `POST /sql/run` is made by a user who is not an Operator, THEN THE SQL_Lab_Backend SHALL reject the request with a forbidden error and SHALL NOT execute the statement.
4. WHEN an Operator submits a statement that the SQL_Guard classifies as allowed, THE SQL_Executor SHALL execute the statement using the `PostgresCopilotExecutor` transaction pattern: issue `SET TRANSACTION READ ONLY`, apply the Statement_Timeout transaction-locally via `set_config('statement_timeout', <value>, true)`, fetch up to Row_Limit + 1 rows internally via `fetchmany` to detect truncation, return at most Row_Limit rows to the caller, and `rollback` the transaction.
5. WHEN a query executes successfully, THE SQL_Lab_Backend SHALL return a Result_Set containing `columns`, `rows`, `rowCount`, `durationMs`, `sql`, and `truncated`.
6. WHEN a query produces more than Row_Limit rows, THE SQL_Lab_Backend SHALL return exactly Row_Limit rows and SHALL set `truncated` to true.
7. WHEN a query produces at most Row_Limit rows, THE SQL_Lab_Backend SHALL return all produced rows and SHALL set `truncated` to false.
8. IF the submitted statement has no explicit row limit, THEN THE SQL_Lab_Backend SHALL enforce Row_Limit on the returned rows.
9. IF a query exceeds the Statement_Timeout, THEN THE SQL_Lab_Backend SHALL roll back the transaction and return a timeout error describing that the Statement_Timeout was exceeded.
10. WHEN a query execution completes, THE SQL_Lab_Backend SHALL report `durationMs` as the measured execution duration in whole milliseconds.
11. IF the SQL_Guard classifies the submitted statement as not allowed, THEN THE SQL_Lab_Backend SHALL return the guard rejection error and SHALL NOT execute the statement.
12. IF the submitted SQL string is empty or contains only whitespace, THEN THE SQL_Lab_Backend SHALL return a validation error and SHALL NOT execute the statement.
13. IF query execution fails for a reason other than timeout, THEN THE SQL_Lab_Backend SHALL roll back the transaction, return an execution error containing the database error message, and SHALL NOT return a Result_Set.

### Requirement 5: SQL Lab tab and query UI (Slice 1)

**User Story:** As an operator, I want a SQL Lab tab with an editor, a Run button, and a results table, so that I can write and run queries visually.

#### Acceptance Criteria

1. THE PrimaryNav SHALL present SQL Lab as a fourth top-level tab alongside the existing Copilot, Observability, and Documents tabs.
2. THE SqlLabPage SHALL present a multi-line SQL editor input with an associated visible text label and a keyboard-operable Run control.
3. WHEN an Operator activates the Run control while the editor contains at least one non-whitespace character, THE SqlLabPage SHALL send the query to `POST /sql/run` through the typed API layer in `frontendkimchi/src/api/`.
4. IF the Operator activates the Run control while the editor is empty or contains only whitespace, THEN THE SqlLabPage SHALL NOT send a request to `POST /sql/run` and SHALL display a visible indication that a non-empty query is required.
5. WHEN a successful Result_Set is received, THE SqlLabPage SHALL render the returned rows in the RowsTable component.
6. WHEN a Result_Set has `truncated` set to true, THE SqlLabPage SHALL display a message, remaining visible for as long as the results are shown, stating that the results were limited to Row_Limit rows.
7. WHILE a query request is in flight, THE SqlLabPage SHALL disable the Run control so that a concurrent request cannot be submitted for the same editor content.
8. THE SqlLabPage SHALL provide keyboard operability for the editor input and the Run control, a visible keyboard focus indicator on each interactive element, and usability at 200% zoom, in conformance with WCAG 2.2 AA as described in PRODUCT.md.
9. WHEN a query request begins, completes successfully, or fails, THE SqlLabPage SHALL announce the corresponding outcome through a polite aria-live region.

### Requirement 6: Honest loading, empty, and error states (Slice 1)

**User Story:** As an operator, I want clear loading, empty, and error feedback, so that I can trust that what I see reflects the real backend result.

#### Acceptance Criteria

1. WHILE a query request is in flight, THE SqlLabPage SHALL display the Skeleton loading state within 200 milliseconds of submission and SHALL NOT display raw text, a blank view, or any prior result set.
2. WHEN a query returns zero rows, THE SqlLabPage SHALL display the EmptyState component with a message indicating the query succeeded and returned no rows, visually distinct from the ErrorState used for failures.
3. IF the SQL_Guard rejects a submitted statement, THEN THE SqlLabPage SHALL display the ErrorState component containing the guard rejection message returned by the SQL_Lab_Backend, and SHALL retain the submitted statement in the editor for correction and resubmission.
4. IF the SQL_Lab_Backend returns a database error, or a query exceeds the Statement_Timeout, THEN THE SqlLabPage SHALL display the ErrorState component containing the returned error message and SHALL retain the submitted statement in the editor.
5. IF a request is rejected as unauthorized or forbidden, THEN THE SqlLabPage SHALL display the ErrorState component with a message indicating that SQL Lab access is restricted to operators.
6. WHEN the SqlLabPage renders a result, THE SqlLabPage SHALL display exactly one of the loading, empty, result, or error states at any given time.

### Requirement 7: Schema sidebar and quick browse (Slice 2)

**User Story:** As an operator, I want a schema sidebar listing tables and columns, so that I can browse the database and quickly select a table without typing SQL.

#### Acceptance Criteria

1. THE SQL_Lab_Backend SHALL expose an endpoint that lists tables and their columns from `information_schema`, restricted to objects for which the SQL_Viewer_Role holds a `SELECT` grant.
2. IF a request to the schema-listing endpoint arrives without a valid Operator JWT, THEN THE SQL_Lab_Backend SHALL reject the request with the same authorization response used by `POST /sql/run` and SHALL NOT return any schema data.
3. THE schema-listing endpoint SHALL NOT return any Sensitive_Table (including `users` and `refresh_tokens`) for which the SQL_Viewer_Role holds no `SELECT` grant.
4. IF the `information_schema` query fails or the database is unreachable, THEN THE SQL_Lab_Backend SHALL respond with an error indicating that the schema listing could not be retrieved and SHALL NOT return a partial table list.
5. WHEN the schema-listing endpoint returns one or more tables, THE SqlLabPage SHALL render each returned table and its columns in a sidebar.
6. WHEN the schema-listing endpoint returns zero tables, THE SqlLabPage SHALL display an indication that no tables are available.
7. IF the schema-listing request from the SqlLabPage fails, THEN THE SqlLabPage SHALL display an error indication and SHALL NOT render a table list.
8. WHEN an Operator selects a table in the sidebar, THE SqlLabPage SHALL replace the editor contents with a statement of the form `SELECT * FROM <table> LIMIT 100`, where `<table>` is the selected table's name.

### Requirement 8: Audit logging of SQL Lab executions (Slice 3)

**User Story:** As an operator and auditor, I want every SQL Lab execution recorded, so that there is an accountable trail of who ran what and when.

#### Acceptance Criteria

1. WHEN a `POST /sql/run` request completes successfully, THE SQL_Lab_Backend SHALL persist exactly one audit record containing the requesting user identity, the submitted SQL, a timestamp recorded in UTC, the execution duration in milliseconds, the returned row count, and a success outcome.
2. WHEN a query execution fails after the SQL_Guard has allowed the statement, THE SQL_Lab_Backend SHALL persist exactly one audit record containing the requesting user identity, the submitted SQL, a timestamp recorded in UTC, the measured execution duration in milliseconds, and an error outcome indicating the failure.
3. WHEN the SQL_Guard rejects a statement, THE SQL_Lab_Backend SHALL persist exactly one audit record containing the requesting user identity, the submitted SQL, a timestamp recorded in UTC, and a rejection outcome.
4. THE audit record SHALL identify the requesting user by the identity supplied in the validated JWT.
5. THE SQL_Lab_Backend SHALL store the submitted SQL in each audit record truncated to at most 10000 characters.
6. IF persisting an audit record fails, THEN THE SQL_Lab_Backend SHALL return an error response indicating the request could not be recorded and SHALL NOT return query result rows to the caller.

### Requirement 9: AI auto-dashboard analysis endpoint (Slice 4)

**User Story:** As an operator, I want an AI-generated dashboard summarizing a result set, so that I can grasp the shape of the data without building charts manually.

#### Acceptance Criteria

1. IF a request to `POST /sql/analyze` arrives without a valid Operator JWT, THEN THE SQL_Lab_Backend SHALL reject the request consistently with the `POST /sql/run` authorization rules and SHALL NOT send any data to the language model.
2. WHEN an Operator requests analysis of a Result_Set, THE SQL_Lab_Backend SHALL send the language model only the column names, inferred column types, the row count, and a sample of at most 20 rows, sending all rows when the Result_Set contains fewer than 20 rows.
3. THE SQL_Lab_Backend SHALL NOT send the full Result_Set beyond the 20-row sample to the language model.
4. THE SQL_Lab_Backend SHALL request the language model output as a Chart_Spec using structured output constrained by a response schema, where the Chart_Spec expresses declarative aggregation instructions (references to column names and a bounded set of allowed aggregation operations) rather than precomputed numeric data points.
5. WHEN the language model returns a Chart_Spec, THE SQL_Lab_Backend SHALL validate the Chart_Spec against the response schema before returning it.
6. IF the language model output fails Chart_Spec schema validation, THEN THE SQL_Lab_Backend SHALL return an error, SHALL NOT return unvalidated content, and SHALL leave the source Result_Set unchanged.
7. THE Chart_Spec SHALL contain only declarative chart and KPI definitions and SHALL NOT contain HTML, JavaScript, or executable code.
8. WHERE no analysis mode is specified, THE SQL_Lab_Backend SHALL use Gemini Flash as the default analysis model.
9. WHERE a deep-analysis mode is selected, THE SQL_Lab_Backend SHALL use the Gemini Pro model for the analysis request.
10. IF the language model is unavailable or does not respond within 60 seconds, THEN THE SQL_Lab_Backend SHALL return an error indicating analysis could not be completed and SHALL leave the source Result_Set unchanged.

### Requirement 10: Auto-dashboard rendering with locally computed numbers (Slice 4)

**User Story:** As an operator, I want the dashboard to display only numbers computed locally from the actual returned rows using a bounded set of allowed aggregations, so that I never see values fabricated or precomputed by the language model.

#### Acceptance Criteria

1. WHEN a validated Chart_Spec is received, THE AutoDashboard SHALL render KPI cards and between one and three charts inclusive using the already-installed recharts library, completing rendering for result sets of up to 10000 rows within 2 seconds.
2. THE Chart_Spec SHALL contain only declarative data instructions that reference column names present in the Result_Set and a bounded set of allowed aggregation operations, namely sum, count, avg, min, max, and optional group-by over named columns, and the language model SHALL NOT emit precomputed numeric values.
3. THE AutoDashboard SHALL compute every displayed numeric value locally from the actual returned rows using only the declared aggregation operations and referenced columns, so that displayed numbers are derived only from real rows and are never invented by the language model.
4. IF a Chart_Spec references a column not present in the Result_Set or an aggregation operation outside the allowed set (sum, count, avg, min, max, group-by), THEN THE AutoDashboard SHALL omit the affected KPI card or chart and SHALL indicate that it could not be computed.
5. THE AutoDashboard SHALL render at most one insight line derived from the Chart_Spec, of at most 200 characters.
6. THE AutoDashboard SHALL provide, for each rendered chart, a semantic table equivalent that presents the same data points, is keyboard-reachable, and is programmatically associated with its chart, in conformance with WCAG 2.2 AA.
7. IF the analysis request fails or returns no valid Chart_Spec, THEN THE SqlLabPage SHALL display a designed error state and SHALL continue to display the underlying Result_Set rows unchanged.
8. WHEN a validated Chart_Spec contains zero chartable data points, THE AutoDashboard SHALL display an empty state while the SqlLabPage continues to display the underlying Result_Set rows.

### Requirement 11: Convenience features (Slice 5, optional/later)

**User Story:** As an operator, I want CSV export, query history, and read-only CTE support, so that repeated exploration is faster.

#### Acceptance Criteria

1. WHERE CSV export is enabled, WHEN an Operator requests export of a non-empty Result_Set, THE SqlLabPage SHALL produce a CSV file whose first row contains the Result_Set column names in displayed order and whose subsequent rows contain every displayed data row in displayed order, with field values quoted and escaped per standard CSV conventions.
2. WHERE CSV export is enabled, IF an Operator requests export while no Result_Set is present or the current Result_Set contains zero rows, THEN THE SqlLabPage SHALL block the export and display a message indicating there is no data to export, leaving the current view unchanged.
3. WHERE query history is enabled, WHEN a query is executed, THE SqlLabPage SHALL persist the submitted SQL to browser localStorage as the most recent entry, retaining at most the 50 most recent submitted statements and evicting the oldest entry when that limit is exceeded.
4. WHERE query history is enabled, IF persisting a submitted statement to browser localStorage fails, THEN THE SqlLabPage SHALL complete query execution normally and display a message indicating that history could not be saved, without discarding the current Result_Set.
5. WHERE query history is enabled, WHEN an Operator selects a history entry, THE SqlLabPage SHALL replace the entire current content of the editor with the stored SQL of the selected entry.
6. WHERE read-only CTE support is enabled, WHEN a submitted statement is a single `SELECT` whose `WITH` clause and all nested sub-queries at every level of the parsed statement tree contain only read-only sub-selects, THE SQL_Guard SHALL classify the statement as allowed.
7. WHERE read-only CTE support is enabled, IF the parsed statement tree contains any data-modifying operation at any level, THEN THE SQL_Guard SHALL reject the statement, refrain from executing it, and return a rejection message indicating that data-modifying operations are not permitted.
