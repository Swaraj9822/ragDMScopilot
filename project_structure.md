# Project Structure — RAG Console (`production-rag`)

> A reference map of this repository for agents and engineers. It explains what
> the project is, how the pieces fit together, where each file lives, and how to
> build, test, and deploy it. Read this first to orient before making changes.

---

## 1. What this project is

**RAG Console** is a single-user, internal engineering tool built on top of a
production-grade **Retrieval-Augmented Generation (RAG) + AI observability**
backend. It gives operators three jobs behind one UI:

1. **Copilot** — ask grounded questions across ingested documents *and* business
   data. An agentic router decides whether a question hits the document RAG
   pipeline, a text-to-SQL database copilot, or both (hybrid), then returns a
   cited answer with a numeric confidence score.
2. **AI Observability** — diagnose latency, errors, routing, retrieval,
   generation, and ingestion via execution **traces** (spans) and correlated
   **logs**.
3. **Documents** — upload/replace source documents and follow their async
   ingestion into Pinecone (parse → chunk → embed → index).

It is a two-part system:

- **Backend** — Python 3.11+ / FastAPI app (`src/rag_system`) plus a separate
  Pub/Sub-driven ingestion **worker**. Package name: `production-rag`.
- **Frontend** — React 18 + TypeScript + Vite SPA (`frontendkimchi`), the
  "Kimchi" RAG Console UI.

The product brief, brand, and design principles live in `PRODUCT.md`. The UI
targets WCAG 2.2 AA.

### External services (data / AI plane)

| Concern | Service |
|---|---|
| Document parsing | LlamaParse (LlamaCloud) |
| Vector search | Pinecone (hybrid dense + BM25 sparse) |
| Embeddings | Google Gemini `gemini-embedding-001` on Vertex AI (GCP) |
| Text generation | Google Gemini on Vertex AI (GCP) |
| Artifact storage | Google Cloud Storage (GCP) |
| Ingestion queue | Google Cloud Pub/Sub (GCP) |
| Auth / trace / log store | PostgreSQL (Neon) |

---

## 2. Top-level layout

```text
c:\aaaa\
├── src/rag_system/          # Backend application package
├── frontendkimchi/          # React + TypeScript SPA (RAG Console UI)
├── tests/                   # Backend pytest + Hypothesis suite
├── config/                  # Copilot text-to-SQL schema catalog (JSON)
├── scripts/                 # Operational helper scripts
├── docs/                    # Design specs / superpowers docs
├── .github/workflows/       # CI (lint+test) and CD (deploy to VM)
├── .kiro/                   # Kiro steering rules and specs
├── .agents/                 # Vendored agent skills (React best practices)
├── main.py                  # Uvicorn import shim (uvicorn main:app)
├── pyproject.toml           # Backend deps, ruff, pytest config
├── requirements.txt         # Pinned runtime deps for the Docker image
├── Dockerfile               # Backend image (shared by api + worker)
├── docker-compose.yml       # Single-host stack: caddy → web → api + worker
├── Caddyfile                # Caddy TLS reverse proxy config
├── nginx.conf (frontend)    # SPA serving + /api proxy (in frontendkimchi/)
├── DEPLOY.md                # GCP VM deployment runbook
├── PRODUCT.md               # Product brief, brand, design principles
├── gcp_setup.md             # GCP setup notes
├── vm_setup.sh              # VM bootstrap script
├── rdscon.py                # Postgres connection helper
└── .env                     # Secrets/config (NOT committed)
```

Generated / tooling directories you can usually ignore: `.hypothesis/`
(Hypothesis DB), `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.tools/`,
`.kimchi/`, `frontendkimchi/node_modules/`, `frontendkimchi/dist/`.

---

## 3. Backend — `src/rag_system/`

FastAPI application. Entry point is `rag_system.api:app`, exposed at repo root
via `main.py` (`uvicorn main:app --reload`). The ingestion worker is a separate
process: `python -m rag_system.worker`.

### 3.1 Core RAG pipeline & query modules

| File | Responsibility |
|---|---|
| `api.py` | FastAPI app: lifespan wiring, middleware (CORS, trace id), and all HTTP routes for documents, queries, ask/stream, traces, and logs. Mounts the auth router and starts flush workers + retention scheduler. |
| `config.py` | `Settings` (pydantic-settings) loaded from `.env`; Pinecone/Gemini/GCP config, GCS + Pub/Sub client factories. `get_settings()` is cached. |
| `models.py` | Core pydantic domain models: `DocumentRecord` (incl. `active_version`, the atomically-published searchable version), `DocumentStatus`, `QueryRequest/Response`, `UnifiedQuery*`, `Citation`, `Chunk`/`RetrievalHit` (version-tagged), feedback + trace records. |
| `service.py` | `RagService` — orchestrates ingestion (parse→chunk→embed→index) and single-pipeline document Q&A; off-request-path query-trace persistence via a bounded thread pool. Ingestion is **versioned and atomically published**: a new version's vectors become searchable only when the record's `active_version` flips, retrieval filters hits to the active version, and superseded/failed versions are garbage-collected. Stale writes from replaced versions raise `StaleIngestionError`; deleted-doc writes raise `DocumentDeletedError` (record writes use GCS compare-and-set). |
| `router.py` | Agentic **query router**: classifies a query as `rag`, `database`, or `hybrid`; fans out and blends results; produces `UnifiedQueryResponse`. |
| `parsing.py` | `DocumentParserRouter` — multi-format parsing (LlamaParse for PDF/DOCX/PPTX/images; native handling for XLSX/CSV/HTML/TXT/MD). |
| `chunking.py` | `DocumentChunker` — token-based sentence chunking via LlamaIndex `SentenceSplitter`. |
| `embedding.py` | `GeminiEmbedder` — embeds chunks with `gemini-embedding-001` on Vertex AI (L2-normalized vectors). |
| `sparse.py` | `BM25SparseEncoder` — BM25 sparse vectors (MS MARCO) for hybrid lexical matching. |
| `retrieval.py` | `PineconeHybridIndex` — upsert + hybrid (dense + sparse) query against Pinecone; version-scoped deletes (`delete_document_version` / `delete_document_except_version`) support atomic-publication cleanup of superseded/partial vectors. |
| `generation.py` | `GroundedAnswerGenerator` — Gemini-backed grounded answer generation with citations; streaming contract uses a `###META###` marker separating prose from trailing JSON. **Fails closed** on unparseable model output: keeps the prose but attaches no citations and marks evidence insufficient rather than crediting every retrieved chunk. |
| `copilot.py` | `DatabaseCopilotService` + `CopilotSqlGuard` — text-to-SQL copilot. The guard is an **AST-based allowlist** (parses SQL with `sqlglot`) that enforces a single read-only aggregating `SELECT`, rejects writes/DDL/CTEs/subqueries/set-ops/window functions, and checks every referenced table *and* column against the schema catalog; invalid SQL raises `SqlValidationError`. Runs approved SQL against the business DB and returns rows. Uses the schema catalog in `config/`. |
| `llm.py` | `TextLLM` protocol + `build_text_llm()` — single Gemini/Vertex abstraction shared by generation, routing, and copilot. |
| `confidence.py` | Deterministic numeric confidence scoring (`[0,1]`) from explainable signals (grounding, logprobs); helpers for RAG, database, and combined scores. |
| `evaluation.py` | Golden-set evaluation harness: `GoldenCase` model + scoring against `tests/golden/rag_golden_set.json`. |
| `storage.py` | `GcsArtifactStore` + object key helpers (parsed docs, chunks, embedding manifests, document records, query traces/feedback). |
| `queue.py` | `PubSubIngestionQueue`, `IngestionJob`, `ReceivedIngestionJob` — Pub/Sub publish/pull/ack for ingestion. |
| `worker.py` | Standalone ingestion worker process: polls Pub/Sub, runs `RagService` ingestion, propagates trace ids, backs off on errors. |
| `observability.py` | Centralised structured logging, per-request trace-id contextvars, token tallies, metrics, and `retry_on_transient` (tenacity) helpers. |
| `rate_limit.py` | In-process sliding-window rate limiter + FastAPI dependency for abuse-prone routes (login/register/refresh). |

### 3.2 Auth subpackage — `src/rag_system/auth/`

Self-managed JWT authentication backed by a Postgres `users` table.

| File | Responsibility |
|---|---|
| `__init__.py` | Public surface: `router`, `get_current_user`, `apply_schema`, `AuthService`, request/response models. |
| `router.py` | Routes: `/auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/me`. |
| `service.py` | `AuthService` — registration / login / lookup orchestration + refresh-token rotation. Bootstrap registration and token rotation are race-safe: only the winner of a concurrent same-token refresh mints a successor pair. |
| `models.py` | `LoginRequest`, `RegisterRequest`, `TokenResponse`, `UserPublic`. |
| `schema.py` | Idempotent `users` table setup, run at startup (`apply_schema`). |
| `store.py` | Postgres user store (persistence); `create_bootstrap_user` atomically creates the first account (advisory lock + `WHERE NOT EXISTS` insert) so registration cannot be raced open when self-service is disabled. |
| `refresh_store.py` | Refresh-token persistence / rotation; `revoke` is an atomic compare-and-set (`WHERE ... revoked_at IS NULL`) returning whether *this* call won the revoke. |
| `passwords.py` | Bcrypt password hashing/verification. |
| `tokens.py` | JWT encode/decode (access + refresh). |
| `dependencies.py` | FastAPI dependencies: `get_auth_service`, `get_current_user`. |

### 3.3 Observability tracing subpackage — `src/rag_system/observability_tracing/`

Additive request-tracing and log-persistence platform (spans + logs → Postgres).

| File | Responsibility |
|---|---|
| `__init__.py` | Public surface + `get_span_recorder()` singleton and `record_query_summary()`. |
| `models.py` | Domain + stored models: `Span`, `Trace`, `SpanStatus`, `AttributeValue`, `LogRecordModel`, `StoredSpan/Trace`. |
| `recorder.py` | `SpanRecorder` — creates/records spans, query-summary attributes (`QUERY_SUMMARY_OPERATION`). |
| `context.py` | Trace/span context propagation via contextvars; `propagate_into_thread`, `bind_span`, `restore_span`. |
| `sampler.py` | `TraceSampler` — enable flag + sample-rate decision. |
| `buffers.py` | `BoundedSpanBuffer` / `BoundedLogBuffer` — bounded in-memory capture with drop metrics. |
| `flush_workers.py` | `TraceFlushWorker` / `LogFlushWorker` — background flush of buffered spans/logs to the store; `group_spans_by_trace`. |
| `trace_store.py` | `PostgresTraceStore` + `TraceSearchFilters` — persist/query traces. |
| `log_store.py` | `PostgresLogStore` + `LogSearchFilters` — persist/query logs. |
| `log_handler.py` | `TracePersistingLogHandler` — logging handler that captures logs onto the active trace. |
| `serializer.py` / `log_serializer.py` | Trace/log (de)serialization boundary + error types. |
| `schema.py` | Trace/log table setup. |
| `retention_scheduler.py` | `RetentionScheduler` — background pruning of old traces/logs. |

### 3.4 HTTP API surface (from `api.py` + `auth/router.py`)

**System / meta**
- `GET /` — service info
- `GET /health` — health probe (used by Docker healthcheck)
- `GET /metrics` — Prometheus-style plaintext metrics

**Documents**
- `POST /documents` — upload (enqueues ingestion)
- `PUT /documents/{document_id}` — replace / re-ingest
- `DELETE /documents/{document_id}` — delete
- `GET /documents/{document_id}` — single record
- `GET /documents` — list

**Queries / Copilot**
- `POST /ask` — unified (routed) query → `UnifiedQueryResponse`
- `POST /ask/stream` — streaming variant (SSE)
- `POST /query` — direct single-pipeline RAG query
- `GET /queries/{trace_id}` — stored query trace record
- `POST /queries/{trace_id}/feedback` — submit feedback
- `POST /copilot/query` — direct database (text-to-SQL) copilot query

**Observability**
- `GET /traces` — search traces (filters: time range, etc.)
- `GET /traces/{trace_id}` — trace + all spans
- `GET /logs` — search logs
- `GET /logs/{trace_id}` — logs correlated to a trace

**Auth** (mounted under `/auth`)
- `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`,
  `POST /auth/logout`, `GET /auth/me`

Most non-auth routes require a bearer token (`Depends(get_current_user)`).

---

## 4. Frontend — `frontendkimchi/`

React 18 + TypeScript + Vite SPA. Dark "ink and signal" theme with light-mode
toggle. Three top-level tabs (Copilot, Observability, Documents) plus a login
screen. State/data via TanStack Query; routing via React Router; markdown via
`react-markdown` + `remark-gfm`; charts via `recharts`; icons via
`lucide-react`.

### 4.1 Tooling / config files

| File | Purpose |
|---|---|
| `package.json` | Scripts (`dev`, `build`, `preview`, `lint`, `typecheck`, `test`, `test:watch`) + deps. App name `rag-console-kimchi`. |
| `vite.config.ts` | Vite config (dev server port 3000 to match backend CORS). |
| `tsconfig*.json` | TypeScript project references (app + node). |
| `.eslintrc.cjs` | ESLint config. |
| `index.html` | SPA entry HTML. |
| `Dockerfile` / `nginx.conf` | Build the SPA and serve it via nginx, proxying `/api` → backend. |
| `.env` / `.env.example` | `VITE_API_BASE_URL` etc. |

### 4.2 Source layout — `frontendkimchi/src/`

| Path | Contents |
|---|---|
| `main.tsx` | React root bootstrap. |
| `app/` | `App.tsx` (auth gate + routes/tabs), `queryClient.ts` (TanStack Query client). |
| `api/` | Typed API layer: `client.ts` (fetch wrapper), `auth.ts`, `copilot.ts`, `documents.ts`, `observability.ts`, `tokenStore.ts`, `types.ts`. |
| `pages/` | Top-level pages: `CopilotPage`, `ObservabilityPage`, `DocumentsPage`, `LoginPage` (each with `.module.css`; several with `.test.tsx`). |
| `components/common/` | Shared UI: `AppShell`, `PrimaryNav`, `PageHeader`, `ThemeToggle`, `ToastRegion`, `ConnectionStatus`, `ConfirmDialog`, `UserMenu`, `EmptyState`, `ErrorState`, `Skeleton`, `StatusBadge`, `RouteBadge`, `CodeBlock`, `CopyButton`, `KeyValueList`, `RelativeTime`, `PageLoading`. |
| `components/copilot/` | `ConversationView`, `Composer`, `AnswerCard`, `ContextRail`, `RowsTable`, `ExamplePrompts`. |
| `components/documents/` | `UploadDropZone`, `UploadQueue`, `DocumentCard`, `TrackDocument`, `IngestionPipeline`. |
| `components/observability/` | `TraceList`, `TraceDetail`, `TraceFilters` (+ `traceFilterUtils`), `SpanWaterfall`, `SpanInspector`, `CorrelatedLogs`, `GlobalLogs`, `LogList`, `IndividualQueries`, `SummaryStrip`, `ViewSwitch`. |
| `hooks/` | `useAuth`, `useTheme`, `useToast`, `useHealth`, `useDocumentStore`, `useDocumentPolling`, `useUploadManager`, `useSelectedDocuments`, `useCopilotHistory`, `useObservabilityPrefs`. |
| `lib/` | Pure utilities: `format`, `fileValidation`, `observability`, `waterfall`, `rows`, `individualQueries`, `status`, `persistence`, `sessionData`, `constants` (several with colocated `.test.ts`). |
| `styles/` | `tokens.css` (design tokens), `global.css`, `modules.d.ts` (CSS-module typing). |
| `test/` | Test infra: `setup.ts`, `server.ts` (MSW), `renderWithProviders.tsx`. |

Styling uses CSS Modules (`*.module.css`) colocated with components.

---

## 5. Tests

### 5.1 Backend — `tests/` (pytest + Hypothesis, `asyncio_mode = strict`)

Configured in `pyproject.toml` (`testpaths=["tests"]`, `pythonpath=["src"]`).
Shared fixtures/doubles: `conftest.py`, `auth_doubles.py`,
`observability_tracing_store_double.py`. Golden data: `tests/golden/rag_golden_set.json`.

Broad coverage groups (property-based tests are suffixed `_properties`):
- **Pipeline**: `test_chunking`, `test_parsing`, `test_generation`,
  `test_confidence`, `test_storage`, `test_router`, `test_copilot`,
  `test_rate_limit`, `test_worker`, `test_service_document_records`,
  `test_index_publication` (atomic version publication + cleanup),
  `test_document_record_race` (delete/ingest CAS races), `test_p3_cleanup`.
- **Auth**: `test_auth_{models,passwords,tokens,schema,service,stores,router,dependencies}`.
- **Observability/tracing**: span lifecycle/hierarchy, sampling, buffers,
  context propagation, trace/log stores, retention, serialization,
  round-trips, search/filter/ordering/validation, metrics parity.
- **Integration**: `test_integration_postgres`, `test_rag_flow_integration`,
  `test_api_ingestion_queue`, `test_golden_eval`,
  `test_performance_latency_budgets`.

Tests requiring live services (Postgres, Pinecone, Vertex AI) self-skip when those
are absent, so the suite runs credential-free in CI.

### 5.2 Frontend

Vitest + Testing Library + MSW, colocated `*.test.ts(x)` next to source
(e.g. `CopilotPage.test.tsx`, `api/client.test.ts`, `lib/format.test.ts`).

> Project testing policy (`.kiro/steering/testing.md`): always add tests with
> new features and regression tests with bug fixes, matching the framework of
> the affected package, and run the relevant suite before reporting done.

---

## 6. Configuration, scripts, and ops

| Path | Purpose |
|---|---|
| `config/copilot_schema_catalog.json` | Business-DB schema catalog powering the text-to-SQL copilot: tables, columns, joins, business rules, example questions. `*.example.json` is the committed template. |
| `scripts/enqueue_pdf.py` | Helper to enqueue a PDF ingestion job onto Pub/Sub. |
| `rdscon.py` | Postgres connection helper. |
| `.env` | All secrets/config (Pinecone, LlamaParse, GCP/Vertex AI, GCS, Pub/Sub, DB, JWT). **Never committed.** |
| `*.log` (root) | Local process logs (`ask-server.*`, `backend-review.*`). |

---

## 7. Build, run, and deploy

### 7.1 Local development

Backend:
```bash
pip install -e ".[dev]"          # install with dev extras
uvicorn main:app --reload        # http://localhost:8000
python -m rag_system.worker      # ingestion worker (separate process)
ruff check .                     # lint
pytest -q                        # tests
```

Frontend (`frontendkimchi/`):
```bash
npm install
npm run dev        # Vite dev server on port 3000
npm run build      # tsc -b && vite build
npm run test       # vitest run
npm run lint
```

### 7.2 Containerized stack — `docker-compose.yml`

Single-host topology (see `DEPLOY.md`):

```
Internet → caddy (TLS 80/443) → web (nginx: SPA + /api proxy) → api (uvicorn :8000)
                                                              ↘ worker (Pub/Sub ingestion)
```

- `api` and `worker` share the same backend image (`Dockerfile`); the worker
  overrides the command to `python -m rag_system.worker`.
- `web` is built from `frontendkimchi/Dockerfile` with `VITE_API_BASE_URL=/api`,
  so the SPA and API are same-origin (no CORS in prod).
- `caddy` terminates TLS (Let's Encrypt) for the `nip.io` hostname in `Caddyfile`.

```bash
docker compose up -d --build
```

### 7.3 CI/CD — `.github/workflows/`

- `ci.yml` — on every push/PR: `ruff check` + `pytest` with coverage, matrix
  over Python 3.11 & 3.12. Uses non-secret placeholder env vars; no live
  infrastructure contacted.
- `deploy.yml` — on push to `main` (or manual dispatch): SSH to the GCP VM,
  `git checkout` the ref, `docker compose up -d --build`, then a health smoke
  check. Gated by the `production` GitHub Environment.

Deployment runbook and GCP setup: `DEPLOY.md`, `gcp_setup.md`, `vm_setup.sh`.

---

## 8. Cross-cutting conventions

- **Config**: everything flows through `rag_system.config.Settings` (env-driven,
  cached via `get_settings()`); don't read env vars directly elsewhere.
- **Resilience**: wrap transient external calls with `retry_on_transient()`
  (tenacity) from `observability.py`.
- **Observability**: use `get_logger(__name__)`; trace context propagates via
  contextvars — when spawning threads, use `propagate_into_thread`. Spans are
  buffered in-memory and flushed asynchronously; they self-disable when tracing
  is off or a trace is unsampled (best-effort, never on the request's hot path).
- **Confidence**: every user-facing answer carries a deterministic numeric
  `confidence_score` in `[0,1]` alongside categorical `evidence_status`.
- **Ingestion versioning**: each upload/replacement stamps a content-hash
  `version`; vectors carry it in their metadata. A version is only "published"
  (searchable) when `DocumentRecord.active_version` flips atomically after a
  successful ingest, so a failed/superseded re-ingest never destroys the last
  good version and readers only ever see published vectors. Record writes are
  guarded by GCS compare-and-set against deleted-resurrection and stale-version
  clobbering.
- **Fail closed**: security- and grounding-sensitive paths refuse rather than
  guess — the SQL guard rejects anything it cannot prove safe, and unparseable
  LLM output yields no citations instead of unverified "grounded" prose.
- **Style**: backend line length 100, ruff target `py311`; `.kiro`, `.agents`,
  `frontendkimchi` are excluded from ruff. Frontend uses CSS Modules and a
  typed API layer.
- **Design/UX**: follow `PRODUCT.md` — honest loading/empty/partial/failure
  states, WCAG 2.2 AA, no fabricated metrics or fake progress.

---

## 9. Quick "where do I…" index

| I want to… | Go to |
|---|---|
| Add/change an HTTP route | `src/rag_system/api.py` (or `auth/router.py`) |
| Change how queries are routed (rag/db/hybrid) | `src/rag_system/router.py` |
| Tune retrieval | `retrieval.py`, `sparse.py` |
| Change answer generation / prompts | `generation.py`, `copilot.py`, `llm.py` |
| Adjust the ingestion pipeline | `service.py`, `parsing.py`, `chunking.py`, `embedding.py`, `worker.py` |
| Work on tracing/logs | `src/rag_system/observability_tracing/` |
| Add/modify auth | `src/rag_system/auth/` |
| Change app settings/env | `src/rag_system/config.py` + `.env` |
| Edit the text-to-SQL schema | `config/copilot_schema_catalog.json` |
| Build UI pages/components | `frontendkimchi/src/pages/`, `frontendkimchi/src/components/` |
| Change frontend API calls | `frontendkimchi/src/api/` |
| Deploy | `docker-compose.yml`, `DEPLOY.md`, `.github/workflows/deploy.yml` |
