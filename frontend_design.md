# Frontend Design: RAG Console

## 1. Purpose

Build a production-quality frontend for the existing `production-rag` backend. The application is
an internal workspace for asking questions across documents and business data, inspecting AI
execution traces, and adding documents to the retrieval corpus.

The top-level navigation must contain exactly three tabs:

1. **Copilot** — ask natural-language questions and inspect the grounded answer.
2. **AI Observability** — search traces and logs, inspect span waterfalls, and diagnose failures.
3. **Documents** — upload documents and follow their ingestion into Pinecone.

This file is the implementation brief. Use the existing FastAPI API in
`src/rag_system/api.py`; do not replace the backend with mocks or duplicate backend behavior in the
browser.

---

## 2. Product Direction

The product should feel like a focused engineering instrument, not a generic marketing dashboard.
Use a dark, low-glare canvas, crisp typography, restrained status color, and dense but readable data
views. The Copilot should feel calm and conversational; Observability should feel exact and
diagnostic; Documents should make a complex ingestion pipeline easy to understand.

Working product name: **RAG Console**

Short descriptor shown under the name: **Copilot · Telemetry · Knowledge**

Target users:

- Business operators asking questions about uploaded content and structured business data.
- Engineers investigating latency, errors, routing, retrieval, generation, and ingestion.
- Knowledge managers uploading and replacing source documents.

This iteration is a single-user/internal tool. The backend currently has no authentication API, so
do not invent login, user, team, billing, notification, or settings screens.

---

## 3. Technical Baseline

Create a new `frontend/` application with:

- React and TypeScript.
- Vite.
- React Router for URL-addressable top-level tabs.
- TanStack Query for server state, polling, caching, and retries.
- Lucide React for icons.
- Recharts only for small data-driven summary charts.
- `react-markdown` with `remark-gfm` for answer rendering.
- Plain CSS modules or a small global CSS token layer. Tailwind is also acceptable if Kiro uses it
  consistently; do not mix two styling systems.
- Vitest and React Testing Library for core interaction tests.

Run the Vite development server on port `3000`, because the backend CORS configuration currently
allows `http://localhost:3000`.

Use:

```text
VITE_API_BASE_URL=http://localhost:8000
```

The frontend must never contain AWS, Pinecone, PostgreSQL, Google, or LlamaParse credentials.

### Recommended frontend structure

```text
frontend/
  src/
    app/
      App.tsx
      routes.tsx
      queryClient.ts
    api/
      client.ts
      types.ts
      copilot.ts
      documents.ts
      observability.ts
    components/
      common/
      copilot/
      documents/
      observability/
    hooks/
    pages/
      CopilotPage.tsx
      ObservabilityPage.tsx
      DocumentsPage.tsx
    styles/
      tokens.css
      global.css
    test/
  .env.example
  vite.config.ts
```

Prefer small domain components over one large page component. Keep API access in `src/api`; page
components must not call `fetch` directly.

---

## 4. Information Architecture

Use URL paths so refresh, browser history, and shared links behave correctly:

| Top-level tab | Route | Icon |
|---|---|---|
| Copilot | `/copilot` | `MessageSquareText` |
| AI Observability | `/observability` | `Activity` |
| Documents | `/documents` | `Files` |

Redirect `/` to `/copilot`.

Support a trace deep link:

```text
/observability?trace=<trace_id>
```

Opening this URL should fetch the requested trace, select it, and open its detail inspector. A
Copilot answer and an uploaded document may use this route to hand an investigation to the
Observability tab.

Do not add a fourth top-level tab. Secondary controls within AI Observability may switch between
**Traces** and **Logs**, but these are local views rather than global navigation.

---

## 5. Global Application Shell

### Desktop

Use a fixed-height top bar and a second navigation row:

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  RAG Console / Copilot · Telemetry · Knowledge       ● API connected    │
├──────────────────────────────────────────────────────────────────────────┤
│  Copilot                 AI Observability                 Documents       │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│                         active page content                              │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

Top bar:

- Left: compact product mark made from three stacked horizontal signal lines, product name, and
  descriptor.
- Right: backend health indicator and a theme button.
- Check `GET /health` on first load and every 30 seconds.
- Connected state: small green dot and text `API connected`.
- Failure state: amber/red dot and text `API unavailable`; clicking opens a small popover with the
  configured base URL and a retry button.

Primary tab row:

- Full-width, no sidebar.
- Use icons plus labels.
- Active state uses a 2 px teal underline and brighter text.
- Tabs must be keyboard reachable and expose the active page with `aria-current="page"`.

Keep content within a `1440px` maximum width, except the trace waterfall and data tables, which may
use the full available viewport width. Use 24 px page padding on desktop.

### Responsive behavior

- Below `900px`, stack all two-column layouts.
- Below `640px`, reduce page padding to 12 px, keep all three top-level labels visible, and allow the
  nav row to scroll horizontally if localization or zoom requires it.
- On mobile, trace detail opens as a full-screen sheet with a visible Back button.
- Tables must scroll horizontally; never compress numeric or identifier columns until unreadable.
- The Copilot composer remains sticky above the mobile safe area.

---

## 6. Visual System

### Design character

Use an “ink and signal” visual language: near-black blue surfaces, warm off-white text, and teal as
the active signal color. Amber and coral are reserved for warnings and failures. Avoid gradients,
glowing neon, glassmorphism, oversized hero text, and decorative charts.

### Color tokens

Implement semantic CSS custom properties. These are the dark-theme values:

```css
:root {
  --bg-canvas: #0b1016;
  --bg-surface: #111923;
  --bg-raised: #17222e;
  --bg-subtle: #0e151e;
  --border-default: #273545;
  --border-strong: #3a4a5d;

  --text-primary: #f3f0e8;
  --text-secondary: #aab4bf;
  --text-muted: #788594;

  --accent: #43d6b5;
  --accent-strong: #1fb391;
  --accent-soft: rgba(67, 214, 181, 0.12);

  --info: #79a9ff;
  --warning: #e9ad55;
  --danger: #ff716c;
  --success: #5bd29a;
  --focus: #a8f4e3;
}
```

Also provide a light theme using the same semantic tokens. Default to the operating-system
preference and persist an explicit user choice in `localStorage`. Status colors must retain WCAG AA
contrast in both themes.

### Typography

- UI and body: **IBM Plex Sans**, with system sans-serif fallbacks.
- IDs, timestamps, durations, SQL, logs, and span attributes: **IBM Plex Mono**, with monospace
  fallbacks.
- Bundle fonts through npm (for example `@fontsource`) rather than depending on a third-party font
  request at runtime.
- Base size: 14 px on desktop and 15–16 px for answer prose.
- Page title: 24 px / 600 weight.
- Section heading: 16 px / 600 weight.
- Metadata: 12 px.

Use sentence case. Avoid all-uppercase body labels; uppercase is acceptable only for short log-level
badges such as `ERROR`.

### Shape, spacing, and elevation

- Spacing scale: `4, 8, 12, 16, 24, 32, 48`.
- Standard radius: 10 px.
- Small controls and badges: 6 px.
- Do not turn every label into a pill. Reserve pills for status, route, and filters.
- Prefer borders and surface shifts to large shadows. Use one restrained shadow only for popovers,
  dialogs, and mobile sheets.
- Minimum interactive target: 40 × 40 px.

### Motion

- Standard transition: 140–180 ms ease-out.
- Use a short opacity/translate transition for inspectors and popovers.
- Trace waterfall bars may animate once from the left when first loaded; do not reanimate during
  every refresh.
- Respect `prefers-reduced-motion` and remove nonessential movement.

---

## 7. Shared Components

Build and reuse:

- `AppShell`
- `PrimaryNav`
- `ConnectionStatus`
- `PageHeader`
- `StatusBadge`
- `RouteBadge`
- `EmptyState`
- `ErrorState`
- `Skeleton`
- `CopyButton`
- `RelativeTime` with an absolute timestamp tooltip
- `ConfirmDialog`
- `ToastRegion`
- `CodeBlock`
- `KeyValueList`

Status language and color:

| Meaning | Values | Treatment |
|---|---|---|
| Good | `success`, `indexed`, `grounded` | green dot/text on subtle surface |
| In progress | `queued`, `parsing`, `chunking`, `embedding` | blue/teal, optional restrained spinner |
| Warning | `partially_grounded`, `insufficient_evidence`, `WARNING` | amber |
| Failure | `failed`, `error`, `ERROR`, `CRITICAL` | coral/red |
| Inactive | `deleted`, unknown | neutral gray |

Never communicate status using color alone. Always include text and, where useful, an icon.

---

## 8. Tab 1 — Copilot

### Goal

Let the user ask one question in natural language. The backend automatically routes it to document
RAG, the database copilot, or both. Make the answer easy to read while keeping its evidence,
generated SQL, tabular rows, and trace identity available for verification.

Use `POST /ask` as the primary endpoint. Do not make the user select RAG versus database in advance;
the backend classifier already owns that decision.

### Layout

Desktop uses a `minmax(0, 1fr) 320px` grid:

- Main column: conversation and sticky composer.
- Context rail: current query options, selected document IDs, and session information.

On a narrow viewport, the context rail becomes a collapsible **Query context** panel above the
composer.

### Empty state

Show a compact introductory block, not a large hero:

- Heading: `Ask across documents and business data`
- Copy: `The copilot will choose document search, database analysis, or both.`
- Four example prompts as buttons:
  - `Summarize the key policy changes in the uploaded documents`
  - `What was total sales this month?`
  - `Which customer generated the most revenue?`
  - `Compare the documented refund policy with recent refund data`

Clicking an example places it in the composer without submitting automatically.

### Composer

- Multi-line textarea that grows from 1 to 6 lines.
- Placeholder: `Ask about your documents or business data…`
- Send button with `ArrowUp`; disabled for empty input and while submitting.
- `Enter` sends; `Shift+Enter` inserts a line break. Document this in an accessible hint.
- Option: `Show generated SQL`, mapped to `include_sql`.
- Selected document IDs appear as removable chips. These IDs come from documents saved in the
  Documents tab. If none are selected, omit `document_ids` so the backend searches the full corpus.
- While a request is active, show `Routing and gathering evidence…` and a non-determinate progress
  line. Do not fake individual pipeline stage completion in this tab.
- Provide a Stop button only if the request is implemented with an `AbortController`; stopping the
  browser request does not imply cancellation on the backend, so label it `Stop waiting`.

Payload:

```json
{
  "question": "What was total sales this month?",
  "document_ids": null,
  "include_sql": true
}
```

### Conversation behavior

The backend has no conversation or message-history API. Treat each submission as an independent
question. Keep the current browser session’s messages in memory and optionally persist the latest
20 question/answer pairs in `localStorage`, clearly labelled `Local history`. Do not send prior
messages as hidden context and do not claim that the Copilot remembers earlier questions.

Include a `New session` action that clears only local UI history after confirmation.

### Answer card

Render:

1. Answer prose using sanitized GitHub-flavored Markdown.
2. A metadata row containing:
   - route badge: `Document`, `Database`, or `Hybrid`;
   - evidence badge;
   - client-measured response time;
   - copyable trace ID.
3. Evidence sections shown only when their corresponding data exists:
   - citations;
   - data sources;
   - SQL;
   - result rows;
   - routing explanation.
4. Actions:
   - Copy answer.
   - `Inspect trace`, linking to `/observability?trace=<trace_id>`.

Route-label mapping:

| API route | UI label |
|---|---|
| `rag` | Document |
| `database` | Database |
| `hybrid` | Hybrid |

Evidence details:

- Citations are numbered cards with title (or `Untitled source`), page range, document ID, and chunk
  ID. A citation cannot open the source because the backend has no document-content/download
  endpoint; provide Copy ID actions instead of dead links.
- Data sources show table name and column names.
- SQL is collapsed by default, shown in a monospace code block with a Copy action. Never execute SQL
  in the browser.
- Rows render in a dynamic table. Derive column order from the first row, then append any keys found
  in later rows. Keep `0`, `false`, and empty strings distinct from `null`. Use `—` only for null or
  missing values. Cap the visible table height and support horizontal scrolling.
- Routing explanation is low-emphasis metadata under `Why this route?`.
- If `insufficient_evidence_reason` exists, show it in an amber callout directly after the answer.

### Copilot states

- Loading: preserve the submitted user message and show an assistant skeleton.
- Empty answer: show `The service returned no answer` with the trace ID when available.
- HTTP 400: show the backend `detail` near the composer.
- HTTP 503: show `The requested AI service is currently unavailable` and preserve the draft for
  retry.
- Network failure: show an inline retry button and mark the global API status if health also fails.
- Never discard typed text because of an error.

---

## 9. Tab 2 — AI Observability

### Goal

Provide a practical investigation surface for the tracing platform already implemented in
`src/rag_system/observability_tracing/`. Users must be able to start from an overview, narrow the
trace list, inspect the hierarchical span timing, and correlate logs without leaving the page.

### Page header

- Title: `AI Observability`
- Subtitle: `Trace routing, retrieval, generation, SQL, and ingestion from request to result.`
- Right actions:
  - Auto-refresh switch, default on.
  - Interval selector: `5s`, `10s`, `30s`; default `10s`.
  - `Refresh now` icon button.
- Pause auto-refresh while the browser tab is hidden.
- Preserve active filters in the URL query string.

### Summary strip

Fetch up to the latest 500 traces for the selected time range and compute summary values in the
browser. These cards are summaries of the loaded sample, not global production totals; label them
`Loaded window`.

Show four compact values:

- Trace count.
- Error rate: error traces / loaded traces.
- p95 duration.
- Slowest route (route with highest average duration, only when it has at least two loaded traces).

Under the values, show one slim route-distribution bar split by route. Do not invent token cost,
model cost, or quality scores that the API does not return consistently.

If more than 500 traces may exist, state `Based on the latest 500 matching traces`.

Requests made by the console can themselves produce `/health`, `/traces`, and `/logs` traces. Add a
`Hide console traffic` switch, on by default, that removes these routes (and `/metrics`) from the
visible list and loaded-window calculations on the client. This is a presentation filter only;
explain it in a tooltip, and allow operators to turn it off when diagnosing the observability
endpoints themselves.

### Local view switch

Use a compact segmented control below the summary:

- **Traces**
- **Logs**

This is not a top-level navigation tab.

### Trace filters

Use an expandable filter bar:

- Time preset: `15 minutes`, `1 hour`, `6 hours`, `24 hours`, `Custom`; default `1 hour`.
- Custom start/end datetime fields, sent as ISO-8601.
- Route: text/select populated from loaded route values, with common options `/ask`, `/query`,
  `/copilot/query`, `/documents`, and `ingestion` when present.
- Status: `All`, `success`, `error`.
- Minimum duration in milliseconds.
- Limit: `50`, `100`, `250`, `500`, `1000`; default `100`.
- `Clear filters`.

Validate end >= start, minimum duration from `0` to `86400000`, and limit from `1` to `1000` before
sending the request. Filters combine with AND semantics.

### Trace master/detail layout

Desktop:

```text
┌──────────────── trace list (44%) ────────────────┬──── detail (56%) ────┐
│ status  route  started  duration  spans  id      │ summary               │
│ selected row                                    │ waterfall             │
│ ...                                             │ span inspector / logs │
└──────────────────────────────────────────────────┴───────────────────────┘
```

Trace list:

- Columns: status, route, start time, duration, span count, shortened trace ID.
- Sort is newest-first as delivered by the backend. Do not imply unsupported server-side sorting.
- Format durations adaptively: `842 ms`, `1.42 s`, `2m 08s`.
- Keep full timestamps and IDs in tooltips.
- Selecting a row updates `?trace=` without pushing excessive browser-history entries.
- New data from auto-refresh should not clear the current selection.
- Visually mark a selected row using background and a 3 px accent edge, not color alone.

Trace detail header:

- Route, root status, total duration, absolute start time, span count, and copyable trace ID.
- `Open logs` scrolls/focuses the correlated log section.
- If the deep-linked trace is not in the current result window, fetch it separately through
  `GET /traces/{trace_id}` and retain it as the selected detail.

### Span waterfall

Build a real waterfall from each span’s `start_ts`, `duration_ms`, `span_id`, and
`parent_span_id`.

- Timeline origin is the earliest span start.
- Bar left offset = `(span start - origin) / trace duration`.
- Bar width = `span duration / trace duration`, with a 2 px minimum width.
- Add a ruler with relative milliseconds.
- Display operation name, duration, status, and depth.
- Determine depth by walking `parent_span_id`; guard against missing parents and cycles.
- Indent labels by depth, capped visually after 6 levels.
- Success bars use teal; failed bars use coral; the selected span uses a high-contrast outline.
- Parallel sibling spans must appear on separate rows with aligned time positions.
- Clicking or keyboard-activating a span opens the span inspector.
- Provide a non-visual accessible representation as a semantic list/table containing every span and
  its timing.
- If trace duration is zero, render all spans with their minimum width rather than dividing by zero.

Span inspector:

- Operation name, status, start timestamp, duration, span ID, parent span ID.
- Attributes rendered as a sorted key/value list.
- Values retain their primitive type; booleans and numbers should not be quoted.
- Exception type/message receives a visible error callout.
- Model identifiers, token counts, retrieval hit counts, scores, evidence status, citation counts,
  and document identifiers should remain ordinary returned attributes rather than hard-coded
  assumptions.

### Correlated logs

When a trace is selected, fetch `GET /logs/{trace_id}` lazily. Show:

- Timestamp with millisecond precision.
- Level badge.
- Logger name.
- Message.
- Expandable exception text.
- Expandable `extra` fields as key/value rows.

Use a virtualized or capped list if the response is large. Logs are already returned newest-first;
provide a local toggle for `Newest first` / `Oldest first` without refetching.

### Global Logs view

The **Logs** local view uses `GET /logs` and contains:

- Start and end time.
- Level filter: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
- Trace ID input with 32-character lowercase hexadecimal validation.
- Limit from 1 to 1000.
- A dense log stream with timestamp, level, logger, message, and shortened trace ID.
- Clicking a valid trace ID returns to the Traces view and selects that trace.
- Search matching nothing is a valid empty result, not an error.

### Empty and failure states

- No traces: `No traces match this window` with a Clear filters action.
- Trace unavailable: `This trace was not found. It may be outside retention.`
- Observability store unavailable: isolate the error inside this tab; Copilot and Documents may
  still work.
- Logs empty for a valid trace: `No persisted logs for this trace`.
- Show partial data if trace detail loads but logs fail.

---

## 10. Tab 3 — Documents

### Goal

Upload supported source files and make the asynchronous path to Pinecone visible:

```text
Browser → FastAPI upload → S3 raw object → SQS job → parse → chunk → embed → Pinecone index
```

The browser must call FastAPI only. It must not connect to Pinecone, S3, SQS, or LlamaParse
directly.

### Page layout

Desktop uses a 5/7 split:

- Left: upload panel and format guidance.
- Right: documents uploaded from this browser, including pipeline progress and actions.

After the first successful upload, the right side becomes visually dominant. On small screens,
upload panel comes first and history follows.

### Upload drop zone

- Heading: `Add knowledge to the corpus`
- Copy: `Files are parsed, chunked, embedded, and indexed for retrieval.`
- Dashed drop zone with `UploadCloud` icon.
- Support drag/drop and file picker.
- Support multiple selection, but send one `POST /documents` request per file so each file gets its
  own record and progress.
- Default client-side maximum: 10 MiB per file, matching the current backend default. The server is
  authoritative and may return 413 if its configured limit differs.

Accepted extensions:

```text
PDF, DOCX, DOC, PPTX, PPT, ODT, RTF, EPUB, XML, XLS, XLSX, CSV,
HTML, HTM, TXT, MD, MARKDOWN, RST,
JPG, JPEG, PNG, TIFF, TIF, BMP, WEBP, GIF
```

Set the file input `accept` value, but also validate extension in TypeScript. Preserve the file in
the pending list when validation fails so the user can see and remove the failure.

Upload progress:

- `fetch` does not provide reliable upload progress. Use `XMLHttpRequest` for the file upload if a
  true byte-progress bar is implemented.
- Otherwise show an indeterminate `Uploading…` state. Never show a fabricated percentage.
- Use multipart form field name `file`.

### Upload queue

For each local file show:

- filename and extension icon;
- human-readable size;
- local upload state;
- server document ID after acceptance;
- version;
- ingestion state;
- error message when present;
- retry/remove action as appropriate.

Do not submit all files simultaneously. Use a concurrency limit of 2 to avoid flooding the API.

### Ingestion progress

`POST /documents` returns HTTP 202 and a `DocumentRecord`. After acceptance, poll
`GET /documents/{document_id}`:

- every 2 seconds for the first 30 seconds;
- every 5 seconds afterward;
- stop on `indexed`, `failed`, or `deleted`;
- stop automatic polling after 10 minutes and show a manual `Check status` action;
- resume polling for nonterminal records restored from local history.

Visual pipeline:

```text
Queued → Parsing → Chunking → Embedding → Indexed
```

Map states exactly:

| API status | Active/completed step |
|---|---|
| `queued` | Queued active |
| `parsing` | Queued complete, Parsing active |
| `chunking` | through Parsing complete, Chunking active |
| `embedding` | through Chunking complete, Embedding active |
| `indexed` | all steps complete |
| `failed` | stop at the last known/current step and show error |
| `deleted` | neutral terminal state |

The backend performs Pinecone upsert after embedding but exposes only the final `indexed` state.
Do not add a fictional separate `Pinecone` status. Use the completion copy
`Indexed in Pinecone`.

### Document history and actions

The backend has no `GET /documents` collection endpoint. Therefore:

- Persist records accepted/uploaded by this browser in `localStorage`.
- Label the section `This browser’s uploads`.
- Do not imply this is the complete corpus.
- Provide `Track an existing document` where a user can enter a known document ID and call
  `GET /documents/{id}`.
- Keep at most 100 local records and order by most recently uploaded/checked.

Each document row/card supports:

- Copy document ID.
- `Use in Copilot`, which saves it to the Copilot selection and navigates to `/copilot`.
- `Replace file`, using `PUT /documents/{document_id}` with a new multipart file.
- `Delete`, using `DELETE /documents/{document_id}` behind a confirmation dialog.
- `Inspect trace` when this browser initiated the upload and retained the 32-character trace ID it
  generated for the `X-Trace-Id` request header. A newly accepted trace may not be queryable until
  the asynchronous trace flush completes; show a useful `Trace is not available yet` state and a
  Retry action. Omit this action for manually tracked documents whose request trace is unknown.

When replacing a document, retain the same document ID and update the displayed version/status from
the returned record.

Deletion confirmation copy:

`Delete “<title>” from the retrieval corpus? This removes its indexed chunks and stored document
record. This action cannot be undone.`

### Documents states

- Empty: `No uploads saved in this browser yet.`
- Unsupported format: list the extension and link/focus the accepted-format disclosure.
- Empty file: show backend detail.
- 413: `This file exceeds the server’s upload limit.`
- Failed ingestion: show the server `error` and allow Replace file; do not blindly retry the same
  completed upload request.
- Local history missing a backend record (404): mark `Not found` and offer Remove from local
  history.

---

## 11. API Contract

### API client rules

- Base all URLs on `VITE_API_BASE_URL`.
- Set `Accept: application/json` for JSON APIs.
- Do not set `Content-Type` manually for `FormData`; allow the browser to create the multipart
  boundary.
- For user-triggered Copilot and upload requests, generate a 32-character lowercase hexadecimal
  trace ID with `crypto.randomUUID().replaceAll("-", "")` and send it as `X-Trace-Id`. This aligns
  with the observability trace lookup format and forces sampling while tracing is enabled.
- Parse FastAPI errors in the form `{ "detail": "..." }`; fall back to a safe generic message.
- Apply request timeouts with `AbortController`: 120 seconds for AI queries and uploads, 15 seconds
  for health/search/detail requests.
- Retry idempotent GET requests up to two times with backoff. Do not automatically retry POST, PUT,
  or DELETE requests.
- Treat an empty array from trace/log search as success.
- Dates sent to the API must be ISO-8601 with timezone information.

### Endpoints used

| Method | Endpoint | Frontend use |
|---|---|---|
| GET | `/health` | global connection state |
| POST | `/ask` | Copilot question |
| POST | `/documents` | upload a new document |
| GET | `/documents/{document_id}` | poll or track ingestion |
| PUT | `/documents/{document_id}` | replace an existing document |
| DELETE | `/documents/{document_id}` | remove a document |
| GET | `/traces` | search/recent trace list |
| GET | `/traces/{trace_id}` | trace deep link/detail |
| GET | `/logs` | global log search |
| GET | `/logs/{trace_id}` | correlated logs |

The `/metrics`, `/query`, `/copilot/query`, and feedback endpoints do not need direct UI controls
for this first frontend. `/ask` already provides the intended unified query experience.

### Required TypeScript shapes

```ts
type DocumentStatus =
  | "queued"
  | "parsing"
  | "chunking"
  | "embedding"
  | "indexed"
  | "failed"
  | "deleted";

interface DocumentRecord {
  id: string;
  title: string;
  version: string;
  s3_uri: string;
  status: DocumentStatus;
  error: string | null;
}

interface BrowserDocumentEntry {
  document: DocumentRecord;
  request_trace_id: string | null;
  added_at: string;
  last_checked_at: string;
}

interface Citation {
  document_id: string;
  chunk_id: string;
  page_start: number | null;
  page_end: number | null;
  title: string | null;
}

interface CopilotDataSource {
  table: string;
  columns: string[];
}

interface UnifiedQueryResponse {
  answer: string;
  route: "rag" | "database" | "hybrid" | string;
  evidence_status: string;
  trace_id: string;
  citations: Citation[];
  confidence: string | null;
  insufficient_evidence_reason: string | null;
  sql: string | null;
  rows: Record<string, unknown>[];
  data_sources: CopilotDataSource[];
  routing_reasoning: string | null;
}

type SpanStatus = "success" | "error";
type AttributeValue = string | number | boolean;

interface Span {
  span_id: string;
  parent_span_id: string | null;
  operation: string;
  start_ts: string;
  duration_ms: number;
  status: SpanStatus;
  attributes: Record<string, AttributeValue>;
}

interface Trace {
  trace_id: string;
  route: string;
  start_ts: string;
  duration_ms: number;
  root_status: SpanStatus;
  spans: Span[];
}

interface LogRecord {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  trace_id: string | null;
  exc_text: string | null;
  extra: Record<string, AttributeValue>;
  insertion_seq: number;
}
```

Treat unknown string values defensively at runtime. Do not crash the whole page if the backend adds
a new route, document status, log level, or span attribute.

---

## 12. State and Persistence

TanStack Query owns server data. Use local React state or a small context for UI-only state; do not
add Redux.

Persist only:

- theme preference;
- current browser’s document records and selected document IDs;
- latest 20 local Copilot question/answer pairs;
- Observability auto-refresh preference and interval.

Version all `localStorage` payloads and recover safely from invalid JSON. Do not persist raw uploaded
file contents, exception stack traces, SQL rows, or arbitrary logs.

Suggested keys:

```text
rag-console:theme:v1
rag-console:documents:v1
rag-console:selected-documents:v1
rag-console:copilot-history:v1
rag-console:observability-preferences:v1
```

---

## 13. Accessibility

- Meet WCAG 2.2 AA contrast and interaction requirements.
- Provide a skip link to main content.
- Every input must have a visible label or an accessible name.
- Use real buttons, links, tables, and form fields rather than clickable `div` elements.
- Manage focus when dialogs and mobile sheets open/close.
- Announce uploads, terminal ingestion changes, query completion, and errors through a polite
  `aria-live` region.
- Do not announce every poll result when status has not changed.
- All trace rows and spans must be operable with the keyboard.
- Tooltips are supplemental only; essential data must remain available without hover.
- Use `aria-busy` on loading regions.
- Ensure charts and the waterfall have adjacent textual/semantic equivalents.
- Support 200% browser zoom without hiding actions.

---

## 14. Security and Data Handling

- Render answer Markdown without raw HTML. Never use unsanitized `dangerouslySetInnerHTML`.
- Display SQL and logs as text, never executable HTML.
- Never expose secrets or `.env` values.
- Do not show full `s3_uri` as a primary field. It is infrastructure metadata; if retained, place it
  in a collapsed technical-details section and render it as text, not as an assumed public link.
- Validate file extension and size client-side for feedback, but trust and display server
  validation as authoritative.
- Do not connect to Pinecone from the frontend.
- Confirmation is required before deletion.
- Avoid placing full questions, answers, logs, or SQL in analytics or console logging.

---

## 15. Performance

- Lazy-load each top-level page.
- Do not load trace/log data until the Observability tab is visited.
- Do not load correlated logs until a trace is selected.
- Memoize waterfall layout calculations.
- Keep auto-refresh from causing layout jumps; preserve table height and selected trace.
- Use skeletons for initial loads and subtle refresh indicators for background loads.
- Consider row virtualization above 250 visible log records.
- Avoid re-rendering Copilot history while the composer text changes.

---

## 16. Testing Expectations

At minimum, add tests for:

1. Top-level navigation and route redirect.
2. Copilot request payload, including SQL option and selected document IDs.
3. Copilot response rendering for RAG, database, and hybrid shapes.
4. FastAPI error parsing and draft preservation.
5. File extension/size validation.
6. Upload queue concurrency and terminal polling behavior with fake timers.
7. Document status-to-pipeline mapping.
8. Trace filter validation and query-string construction.
9. Span hierarchy/depth calculation, including missing parent and zero-duration traces.
10. Trace deep-link behavior.
11. Empty trace and log results.
12. Keyboard activation of trace rows and spans.

Use MSW or an equivalent request interceptor in tests. Development and production builds must not
ship mock handlers or mock records.

---

## 17. Implementation Sequence

1. Scaffold `frontend/`, configure port 3000, environment handling, routing, query client, global
   tokens, and the application shell.
2. Implement the typed API client and shared loading/error/status components.
3. Build Copilot end to end against `POST /ask`.
4. Build Documents upload, polling, local history, replace, and delete flows.
5. Build Observability trace search, list/detail, waterfall, correlated logs, and global logs.
6. Add responsive layouts, keyboard/focus behavior, reduced motion, and light theme.
7. Add tests, run typecheck/lint/test/build, and fix all failures.

Do not require backend modifications for the initial frontend. If a feature cannot be implemented
from the current contract—such as listing every document in the corpus or opening citation
content—show an honest constrained experience described in this document instead of inventing data.

---

## 18. Definition of Done

The frontend is complete when:

- It has exactly three clear top-level tabs: Copilot, AI Observability, and Documents.
- All three tabs use live FastAPI endpoints with no production mock data.
- A user can ask a question, identify its route/evidence, inspect returned SQL/rows/citations when
  present, and open the trace.
- A user can filter traces, select one, understand its span hierarchy and timing, and inspect
  correlated logs.
- A user can upload supported files, see truthful upload/ingestion states, and know when each file
  is indexed in Pinecone.
- Replace and delete document actions work with confirmation and useful errors.
- Loading, empty, network-failure, backend-failure, and partial-data states are designed rather than
  left as raw text.
- The interface works from mobile width through large desktop, at 200% zoom, and by keyboard.
- Typecheck, lint, unit tests, and production build pass.
- No cloud/database credentials, direct Pinecone code, fake metrics, fake progress, or unsupported
  backend assumptions exist in the frontend.
