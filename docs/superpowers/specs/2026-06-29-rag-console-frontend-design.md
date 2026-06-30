# RAG Console Frontend — Design Spec

## 1. Goal & Scope

Build a single-page application that makes the Production RAG backend legible to internal operators through three tabs:

- **Copilot** — ask grounded questions, see the route taken (RAG vs. database), read the answer, inspect citations, SQL, and result rows.
- **Observability** — search traces by time/route/status/duration, inspect a trace waterfall of spans, and read correlated logs.
- **Documents** — upload/replace source documents, watch ingestion status, and manage the corpus.

This is a single-user internal tool. No auth, billing, or notifications.

## 2. Users & Context

Internal operators and engineers who need to answer, diagnose, and curate without leaving the console. They expect dense, precise data, honest loading/failure states, and evidence (route, trace, citations, SQL, rows) one glance from the answer.

## 3. Architecture & Stack

| Layer | Choice | Rationale |
|---|---|---|
| Build tool | Vite 5+ | Fast HMR, simple config, SPA-optimized |
| Framework | React 18+ (TypeScript) | Mature, widely supported, good shadcn/ui support |
| Routing | TanStack Router | Type-safe file-based routing, excellent dev UX |
| Server state | TanStack Query (React Query) | Caching, deduping, retries, background refetching |
| Styling | Tailwind CSS 3.4+ | Utility-first, consistent token system |
| Components | shadcn/ui | Accessible primitives; we own the source |
| Streaming | EventSource + custom parser | Backend emits SSE on `/ask/stream` |
| Icons | Lucide React | Consistent, lightweight |
| Testing | Vitest + React Testing Library + Playwright | Unit + e2e smoke |

### Color system (matches PRODUCT.md)
- Surface: near-black blue (`slate-950` base)
- Primary signal: teal (`teal-500`)
- Warning: amber (`amber-500`)
- Failure: coral/red (`red-500`)
- Text: slate-50/300/400, AA-compliant

## 4. Project Structure

```
frontend/
  src/
    api/                    # Backend client, streaming parser, types
      client.ts
      types.ts
      streaming.ts
    components/
      ui/                   # shadcn primitives
      layout/
        AppShell.tsx
        TabNav.tsx
      feedback/
        LoadingState.tsx
        EmptyState.tsx
        ErrorState.tsx
      data/
        Timestamp.tsx
        Duration.tsx
        CodeBlock.tsx
        DataTable.tsx
        Badge.tsx
    hooks/
      useTraceSearch.ts
      useLogSearch.ts
      useDocuments.ts
      useStreamingAsk.ts
    routes/
      __root.tsx
      copilot.tsx
      observability.tsx
      documents.tsx
    features/
      copilot/
        CopilotTab.tsx
        ChatInput.tsx
        MessageList.tsx
        CitationCard.tsx
        EvidencePanel.tsx
      observability/
        ObservabilityTab.tsx
        TraceSearchForm.tsx
        TraceTable.tsx
        TraceWaterfall.tsx
        LogPanel.tsx
      documents/
        DocumentsTab.tsx
        UploadDropzone.tsx
        DocumentList.tsx
        DocumentStatusBadge.tsx
    lib/
      utils.ts
      theme.ts
    App.tsx
    main.tsx
  public/
  index.html
  package.json
  tsconfig.json
  tailwind.config.ts
  vite.config.ts
```

## 5. Data Flow & State Management

- **Server state** is owned by TanStack Query:
  - Query traces, logs, documents
  - Mutations for upload, update, delete, feedback
- **Local UI state** uses React hooks:
  - Active trace selection
  - Chat message history
  - Form inputs
- **Streaming** uses a custom `useStreamingAsk` hook that:
  - Opens `EventSource` to `/ask/stream`
  - Emits typed events: `meta`, `status`, `delta`, `final`, `error`
  - Accumulates answer text and status updates
  - Closes cleanly and handles errors
- **No global state library** (Redux/Zustand) — tabs are mostly independent and server state covers the rest.

## 6. API Mappings

### Copilot
- `POST /ask` — non-streaming unified query
- `POST /ask/stream` — streaming unified query (SSE)
- `POST /queries/{trace_id}/feedback` — thumbs/rating

### Observability
- `GET /traces?start=&end=&route=&status=&min_duration_ms=&limit=` — search traces
- `GET /traces/{trace_id}` — get trace with spans
- `GET /logs?start=&end=&level=&trace_id=&limit=` — search logs
- `GET /logs/{trace_id}` — logs correlated to trace

### Documents
- `POST /documents` — upload document (multipart)
- `GET /documents/{document_id}` — get document record
- `PUT /documents/{document_id}` — update/replace document
- `DELETE /documents/{document_id}` — delete document
- **`GET /documents` — *required backend addition* — list all document records**

## 7. Component Design

### App shell
- Fixed top navigation with three tab links
- Teal underline/background for active tab
- Main content area with consistent padding
- Skip link for accessibility

### Copilot tab
- **Chat input** at bottom, multiline, send on Enter
- **Message list** above, newest at bottom
- Each assistant message shows:
  - Route badge (`rag`, `copilot`, `unified`)
  - Evidence status badge
  - Streaming answer text
  - Citations as small cards
  - Expandable SQL + result table (if present)
  - Feedback buttons (1-5 + comment)
- **Trace link** in every assistant message footer → opens Observability with that trace pre-selected

### Observability tab
- **Search panel** on top: time range, route select, status select, min duration, limit
- **Trace table** below: start time, route, duration, status
- **Trace detail drawer** on row click:
  - Waterfall chart of spans (left-aligned bars, hierarchical indentation)
  - Each span shows operation, duration, status
  - Correlated logs panel beneath the waterfall
- **Keyboard support**: arrow keys navigate rows, Enter opens detail, Escape closes drawer

### Documents tab
- **Upload dropzone** at top
- **Document table**: title, version, status, updated time, actions
- **Status badge**: queued, parsing, chunking, embedding, indexed, failed, deleted
- **Actions**: update (file picker), delete (confirm dialog)
- Polling for status updates while documents are not terminal

## 8. Accessibility

- WCAG 2.2 AA target
- Keyboard-operable tables, buttons, and inputs
- Visible `:focus-visible` rings in teal
- `aria-live="polite"` for streaming status and async outcomes
- Semantic headings and landmark regions
- Waterfall chart has a textual alternative (nested list of spans)
- `prefers-reduced-motion` honored for animations
- Color never alone: status icons accompany colored badges

## 9. Error / Loading / Empty States

Every async surface has designed states:
- `LoadingState`: spinner + short label
- `EmptyState`: icon + explanatory text + CTA if appropriate
- `ErrorState`: error message + retry button
- Honest partial states: streaming shows partial answer with a "thinking" indicator

## 10. Testing Strategy

- **Vitest + RTL**: unit tests for streaming parser, API client, hooks, pure components
- **Playwright**: e2e smoke tests for navigation, Copilot submit, trace search, document upload
- **a11y**: `axe-core` assertions in component tests
- **Mock server**: MSW (Mock Service Worker) for backend endpoints in tests

## 11. Required Backend Change

The backend has no `GET /documents` list endpoint. The Documents tab needs this to enumerate the corpus. Add:

```python
@app.get("/documents", response_model=list[DocumentRecord])
def list_documents() -> list[DocumentRecord]:
    return list(get_service()._documents.values())
```

This is the only backend change planned.

## 12. Open Questions

1. Do you approve the Vite + TanStack stack, or do you want Next.js App Router?
2. Are you okay with the single required backend change (`GET /documents`)?
3. Should the Copilot tab default to streaming (`/ask/stream`) or non-streaming (`/ask`)?
