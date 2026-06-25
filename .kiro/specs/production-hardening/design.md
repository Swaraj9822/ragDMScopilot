# Design — Production Hardening

## Overview

This design implements the ten requirements as a set of incremental, independently
shippable changes on the existing architecture. No module is rewritten; each change
slots into the current layering (`api.py` thin layer, `Settings`-driven DI, observability
helpers). The work is grouped into seven areas that map to the requirements:

| Area | Requirements | Primary files |
|------|--------------|---------------|
| CI/CD gate | 1 | `.github/workflows/deploy.yml`, new `ci.yml` |
| Catalog out of git | 2 | `.gitignore`, `config.py`, `copilot.py`, `README.md` |
| DB read-only + conninfo | 3 | `copilot.py`, `config.py` |
| Queue poison handling | 4 | `queue.py`, `worker.py`, `config.py` |
| Health & circuit breaking | 5, 6 | `api.py`, `observability.py`, `config.py` |
| Cost/latency + SQL scoping | 7, 8 | `generation.py`, `copilot.py`, `config.py` |
| Tests & hygiene | 9, 10 | `tests/`, `docker-compose.yml`, `config.py` |

Guiding constraints (from steering): all config flows through `rag_system.config.Settings`
with explicit `Field(alias=...)`; use the observability helpers (`get_logger`, `metrics`,
`timed`, `retry_on_transient`); keep endpoints async and offload blocking work with
`run_in_threadpool`; never read `os.environ` directly in feature code.

## Architecture

The change preserves the existing layering and introduces no new services:

```
Client → FastAPI (api.py: middleware, /health, /ready, endpoints)
          ├─ AgenticRouter (router.py) ── BedrockQueryClassifier
          ├─ RagService (service.py) ── Parser/Chunker/Embedder/Sparse/Index/Generator
          └─ DatabaseCopilotService (copilot.py) ── SqlGuard, PostgresExecutor, Bedrock
observability.py: get_logger, metrics, timed, retry_on_transient, +CircuitBreaker (new)
config.py: Settings (extended), get_settings()
Worker (worker.py) ← SQS (queue.py, +receive-count) → S3 (storage.py)
CI/CD: ci.yml (new gate) + deploy.yml (OIDC + scan)
```

New cross-cutting building blocks: a `CircuitBreaker` in `observability.py` shared by
external-call sites, a readiness aggregator in `api.py`, and an extended `Settings`. All
other changes are localized edits within existing modules.

---

## Components and Interfaces

The following sections (1–8) detail each component change, its interface, and rationale.
Summary of new/changed interfaces:

- `observability.CircuitBreaker` / `@circuit(name, failure_threshold, recovery_timeout_s)` — new; raises `CircuitOpenError`.
- `api.py` `GET /ready` — new readiness endpoint returning `{dependency: status}` and 200/503.
- `DatabaseCopilotService.catalog` — extended resolution (local file → S3 URI).
- `PostgresCopilotExecutor` — autocommit pool + explicit `BEGIN READ ONLY`; `make_conninfo`.
- `SqsIngestionQueue.receive` / `ReceivedIngestionJob.receive_count` — receive-count surfaced.
- `RagService.mark_failed(document_id, error)` — new helper.
- `CopilotSqlGuard.validate` — table-scoped column resolution.
- `Settings` — new aliased fields (see configuration summary).

---

## 1. CI/CD safety gate (Req 1)

Split CI into two workflows:

- **`.github/workflows/ci.yml`** — triggers on `pull_request` and on `push` to `main`.
  Steps: checkout → setup Python 3.11 → `pip install -e .[dev]` → `ruff check .` →
  `ruff format --check .` → `pytest`. This is the gate.
- **`.github/workflows/deploy.yml`** — keep deploy logic but make it depend on CI success.
  Either (a) add the lint/test steps as earlier steps in the existing `deploy` job before
  the build step, or (b) gate via `needs:` on a reusable workflow. Preferred: run lint/test
  as the first steps inside the deploy job so a failure aborts before build/push (satisfies
  1.2, 1.3 with the least moving parts).

Changes to deploy.yml:
- Add `permissions: id-token: write, contents: read` and replace the static-key
  `configure-aws-credentials` inputs with `role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}`
  using OIDC (Req 1.5). Document the IAM role + trust policy in README; provisioning is external.
- After the ECR login/build, add an image scan step (Trivy action, or rely on ECR
  enhanced scanning and poll the findings). Trivy in CI is simplest and self-contained (Req 1.4).
- `wait-for-service-stability: true` already fails the job on instability — keep it, and
  do not add `continue-on-error` (Req 1.6).

Rollback note: documented as "re-run deploy pinned to the previous image tag / previous
task-definition revision." No code needed; capture it in README runbook section.

## 2. Catalog out of version control (Req 2)

- Add `config/copilot_schema_catalog.json` to `.gitignore`; `git rm --cached` it (the file
  stays on disk locally). Keep the tracked `config/copilot_schema_catalog.example.json`.
- New settings in `config.py`:
  - `copilot_schema_catalog_s3_uri: str | None = Field(default=None, alias="COPILOT_SCHEMA_CATALOG_S3_URI")`
- Loader change in `copilot.py` `DatabaseCopilotService.catalog`:
  - Resolution order: local file at `copilot_schema_catalog_path` if it exists → else S3
    object at `copilot_schema_catalog_s3_uri` (parsed into bucket/key, fetched via
    `S3ArtifactStore.get_bytes` or a small boto3 client) → else raise the existing clear
    `FileNotFoundError` with guidance.
  - Preserve the current mtime-based hot-reload for the local-file case. For the S3 case,
    cache in memory (no mtime polling; reload on process restart) to keep it simple.
- README: add a "Schema catalog" section covering local file vs. `COPILOT_SCHEMA_CATALOG_S3_URI`.

Rationale: keeps local DX (just drop the file in `config/`) while removing the production
data model from git and enabling secret-store-backed delivery in prod.

## 3. Read-only enforcement + safe conninfo (Req 3)

Root cause: `PostgresCopilotExecutor.execute` issues `BEGIN READ ONLY`, but the
`psycopg_pool.ConnectionPool` hands out connections with `autocommit=False`, so psycopg has
already opened a read-write transaction before our `BEGIN` runs — the directive is ignored.

Fix:
- Create the pool with `kwargs={"row_factory": dict_row, "autocommit": True}`.
- In `execute`, explicitly manage the transaction on the autocommit connection:
  ```
  with pool.connection() as conn:
      conn.execute("BEGIN READ ONLY")
      if user_id: conn.execute("SELECT set_config('app.current_user_id', %s, true)", (user_id,))
      conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
      rows = conn.execute(sql).fetchmany(max_rows)
      conn.execute("ROLLBACK")
  ```
  With autocommit on, our `BEGIN READ ONLY` is the statement that opens the transaction, so
  the read-only property genuinely applies. A write SQL then raises
  `psycopg.errors.ReadOnlySqlTransaction`.
- Alternative considered: set `default_transaction_read_only=on` via the pool `configure`
  callback. Equivalent guarantee; the explicit-BEGIN approach is chosen because it keeps the
  per-query session settings (`statement_timeout`, `app.current_user_id`) together and visible.
- Build `conninfo` with `psycopg.conninfo.make_conninfo(host=..., port=..., dbname=..., user=..., password=..., sslmode=...)` (Req 3.4).

Test: a unit/integration test that runs an `UPDATE`/`INSERT` through the executor against a
read-only transaction and asserts the DB raises (Req 3.5). Where a live PG is unavailable in
CI, gate it behind a marker and provide a fake connection that asserts `BEGIN READ ONLY` is
the first statement issued.

## 4. Queue poison handling (Req 4)

- `Settings`: `ingestion_max_receive_count: int = Field(default=5, alias="RAG_INGESTION_MAX_RECEIVE_COUNT")`.
- `SqsIngestionQueue.receive`: request the `ApproximateReceiveCount` attribute
  (`AttributeNames=["ApproximateReceiveCount"]`) and carry it on `ReceivedIngestionJob`
  as `receive_count: int`.
- `IngestionWorker._process_message`: on failure, if `receive_count >= max_receive_count`,
  treat the message as abandoned:
  - delete it from the queue (so it stops cycling),
  - persist the `DocumentRecord` as `failed` with the error (reuse the service's failure
    persistence — add a small `RagService.mark_failed(document_id, error)` helper),
  - emit `metrics.increment("rag_ingestion_jobs_abandoned_total")`.
  Otherwise keep current behavior (leave on queue for retry).
- This provides in-code protection (Req 4.2/4.3) and is complementary to an SQS redrive
  policy + DLQ, which remains the preferred primary mechanism and is documented for the
  operator (Req 4.1/4.5). Document the recommended `maxReceiveCount` to match
  `RAG_INGESTION_MAX_RECEIVE_COUNT`.

## 5. Dependency-aware health (Req 5)

- Keep `GET /health` as **liveness**: returns `{"status": "ok"}` immediately (Req 5.1).
- Add `GET /ready` as **readiness** (Req 5.2–5.4):
  - Runs each dependency probe in a thread (via `run_in_threadpool`) with a short timeout
    (`asyncio.wait_for`, ~3s each, configurable `readiness_probe_timeout_s`):
    - S3: `head_bucket` on the configured bucket.
    - Pinecone: `describe_index_stats` (cheap) on the index.
    - Bedrock: lightweight check — region/client construction + a cheap `list`/no-op; if no
      cheap call exists, verify client init and mark "configured" rather than calling the model
      (avoid per-probe model cost). Document this limitation.
    - PostgreSQL (only WHEN copilot DB settings present): `SELECT 1` via the pool.
  - Returns 200 with a per-dependency map when all pass; 503 with the failing dependency(ies)
    named otherwise.
- Container/compose `HEALTHCHECK` continues to target `/health`; orchestration readiness
  (ECS target group / k8s readinessProbe) targets `/ready` (Req 5.5; documented).

## 6. Circuit breaker (Req 6)

Add a small, dependency-free circuit breaker to `observability.py` alongside the retry
helper so all external-call sites can share it.

- `class CircuitBreaker` with states CLOSED/OPEN/HALF_OPEN, parameters
  `failure_threshold`, `recovery_timeout_s`, thread-safe via a lock.
- Expose a decorator `@circuit(name=..., ...)` and/or a registry keyed by provider name so
  Bedrock and Pinecone each have their own breaker instance.
- Compose with the existing `retry_on_transient`: the breaker wraps the retrying call so
  that once the breaker is OPEN, calls fail fast **without** entering the retry/backoff loop
  (Req 6.3). Order: `@circuit(...)` outermost, `@retry_on_transient()` inner.
- On OPEN, raise a dedicated `CircuitOpenError`; map it in `api.py` exception handling to
  HTTP 503 (extend the existing `except (FileNotFoundError, RuntimeError)` handlers).
- `Settings`: `circuit_failure_threshold`, `circuit_recovery_timeout_s` (aliased).
- Emit metrics on every state transition (`rag_circuit_state_total{provider,state}`) and log
  transitions (Req 6.5).

## 7. Cost/latency control + SQL column scoping (Req 7, 8)

### Context budget (Req 7.1)
- `Settings`: `generation_max_context_chars: int = Field(default=24000, alias="RAG_GENERATION_MAX_CONTEXT_CHARS")`.
- In `BedrockNemotronGenerator.answer`, before building the prompt, sort hits by score
  (already ordered) and accumulate chunk text until the budget is reached; drop the
  remainder and log how many were dropped (`metrics.observe("rag_generation_context_dropped_chunks", ...)`).
  Citations should reflect only the chunks actually included.

### Fan-out visibility (Req 7.3/7.4)
- Increment `rag_model_calls_total{path}` at each Bedrock call site, and on the hybrid path
  record `rag_ask_model_calls` per request so fan-out is measurable.
- The SQL-generation retry loop already bounds attempts via `max_attempts`; promote it to a
  `Settings` value `copilot_sql_max_attempts` (Req 7.2).
- Avoid duplicate deterministic calls within a request where cheap to do so (e.g. reuse the
  selected-tables result already passed into `generate_sql`).

### SQL guard column scoping (Req 8)
Replace the union-based column check in `CopilotSqlGuard.validate` with table-scoped
validation using sqlglot's scope/qualification:
- Use `sqlglot.optimizer.qualify.qualify` (or `scope` traversal) to resolve each `Column`
  to its source table given the FROM/JOIN aliases.
- For each resolved `(table, column)`, verify the column is in
  `catalog.column_names_for(table)`. Reject if the column resolves to an unapproved/unknown
  table or cannot be unambiguously resolved (Req 8.1/8.2).
- Keep all existing rejections (unapproved table, `SELECT *`, write/DDL via `find_all`,
  aggregate-required) intact (Req 8.3).
- If full qualification proves brittle on some inputs, fall back to: qualified columns
  validated against their qualifier's table; unqualified columns validated against the union
  **only when a single table is in scope**, otherwise rejected as ambiguous. This preserves
  safety without over-rejecting common single-table queries.

## 8. Tests and hygiene (Req 9, 10)

### Tests (mirror module names per steering)
- `tests/test_storage.py` — extend: mock the boto3 S3 client (botocore Stubber or a fake
  client), assert `put_raw`/`get_json` round-trip, SSE headers chosen correctly, and
  `get_json` returns `None` on `NoSuchKey`.
- `tests/test_generation.py` — extend: mock the Bedrock client; assert citations are built
  from hits, evidence status is `grounded`/`insufficient_evidence`, and context-budget
  truncation drops the lowest-scoring chunks.
- `tests/test_worker.py` — add: chunker/embedder failure leaves message for retry; receive
  count over the limit abandons the message, deletes it, marks the record failed, and emits
  the abandoned metric.
- `tests/test_copilot.py` — add: cross-table column spoofing rejected; CTE/subquery/UNION
  cases; read-only transaction rejects a write (fake connection asserting statement order).
- `tests/test_service_document_records.py` — add: conflicting record updates (last-writer
  semantics documented/asserted).

### Hygiene (Req 10)
- `config.py`: add aliased fields for `pinecone_upsert_batch_size` (default 100),
  `generation_max_tokens` (default 4096), `embedding_max_workers` (existing default), and
  wire them into `retrieval.py`, `generation.py`, `embedding.py` respectively. Defaults match
  current behavior (Req 10.3).
- `docker-compose.yml`: remove the `version: '3.8'` line; change `api.depends_on` to the
  long form with `condition: service_healthy` and add a healthcheck to the worker (e.g. check
  the `/tmp/worker-healthy` file the worker already touches).
- Update README/steering with all new env vars (Req 2.5, 4.5, 5.5, 10.4).

---

## Cross-cutting: configuration summary

New `Settings` fields (all with safe defaults preserving current behavior):

| Field | Alias | Default |
|-------|-------|---------|
| `copilot_schema_catalog_s3_uri` | `COPILOT_SCHEMA_CATALOG_S3_URI` | `None` |
| `ingestion_max_receive_count` | `RAG_INGESTION_MAX_RECEIVE_COUNT` | `5` |
| `readiness_probe_timeout_s` | `RAG_READINESS_PROBE_TIMEOUT_S` | `3` |
| `circuit_failure_threshold` | `RAG_CIRCUIT_FAILURE_THRESHOLD` | `5` |
| `circuit_recovery_timeout_s` | `RAG_CIRCUIT_RECOVERY_TIMEOUT_S` | `30` |
| `generation_max_context_chars` | `RAG_GENERATION_MAX_CONTEXT_CHARS` | `24000` |
| `copilot_sql_max_attempts` | `COPILOT_SQL_MAX_ATTEMPTS` | `3` |
| `pinecone_upsert_batch_size` | `RAG_PINECONE_UPSERT_BATCH_SIZE` | `100` |
| `generation_max_tokens` | `RAG_GENERATION_MAX_TOKENS` | `4096` |
| `embedding_max_workers` | `RAG_EMBEDDING_MAX_WORKERS` | `10` |

## Data Models

No persisted schema changes. In-memory/transport model changes only:

- **`ReceivedIngestionJob`** (`queue.py`): add `receive_count: int = 0`, populated from the
  SQS `ApproximateReceiveCount` attribute.
- **`DocumentRecord`** (`models.py`): unchanged shape; the `failed` status path is reused for
  abandoned messages (error string set, no new fields).
- **Readiness response** (new, `api.py`): `{"status": "ok"|"degraded", "dependencies": {"s3": "ok", "pinecone": "ok", "bedrock": "ok", "postgres": "ok"|"skipped"|"error: ..."}}`.
- **Circuit state** (new, `observability.py`): in-process only — `{name: {state, failure_count, opened_at}}`; not persisted.
- **Catalog models** (`copilot.py` `CopilotSchemaCatalog` etc.): unchanged; only the load
  source changes.

## Correctness Properties

### Property 1: Read-only invariant
For every Copilot query, the executing transaction is read-only; any write/DDL is rejected by
PostgreSQL regardless of guard behavior (Req 3).
**Validates: Requirements 3.1, 3.2**

### Property 2: Column-scope invariant
An accepted query references only `(table, column)` pairs that exist together in the catalog;
a column valid for table A cannot satisfy a query over table B (Req 8).
**Validates: Requirements 8.1, 8.2**

### Property 3: Bounded retries
No message is received more than `ingestion_max_receive_count` times before being abandoned;
no SQL generation exceeds `copilot_sql_max_attempts` (Req 4, 7).
**Validates: Requirements 4.2, 7.2**

### Property 4: Fail-fast invariant
While a provider circuit is OPEN, no retry/backoff is attempted for that provider (Req 6).
**Validates: Requirements 6.1, 6.3**

### Property 5: Budget invariant
The generation prompt context never exceeds `generation_max_context_chars`, and citations
correspond exactly to included chunks (Req 7).
**Validates: Requirements 7.1**

### Property 6: Default-preserving
With an unchanged `.env`, all new settings take defaults that reproduce current behavior
(Req 10).
**Validates: Requirements 10.3**

## Error Handling

- **Readiness probes:** each probe is wrapped in `asyncio.wait_for`; a timeout or exception is
  caught, recorded as that dependency's error, and yields an overall 503 — the probe itself
  never raises out of the endpoint.
- **Circuit open:** `CircuitOpenError` is caught in `api.py` and mapped to HTTP 503 with a
  clear message; it is not retried.
- **Abandoned ingestion:** failure over the receive limit is logged at ERROR with
  `document_id`, the record is marked `failed`, the message is deleted, and a metric is
  emitted — no exception escapes the worker loop (which already backs off on unhandled errors).
- **Read-only violation:** a write attempt surfaces `psycopg.errors.ReadOnlySqlTransaction`,
  which flows through the existing Copilot retry/`SqlValidationError` path and ultimately a
  4xx/5xx to the caller; the transaction is rolled back.
- **Catalog load failure:** if neither local file nor S3 URI yields a catalog, the existing
  descriptive `FileNotFoundError` is raised and mapped to HTTP 503 by `api.py`.

## Testing strategy

- Unit tests run with no network: AWS clients are stubbed/faked; Bedrock and Pinecone are
  mocked. PostgreSQL read-only behavior is asserted via a fake connection that records the
  statement order, with an optional live-PG integration test behind a pytest marker.
- The CI gate (Req 1) runs `ruff` + `pytest` on every PR and on `main` before deploy, which
  makes the rest of this spec self-protecting.
- Each requirement has at least one corresponding test or a CI assertion; see tasks.md for
  the explicit mapping.

## Risks and mitigations

- **Readiness probe cost (Bedrock):** avoid per-probe model invocation; verify client/config
  only. Documented limitation.
- **SQL qualification brittleness:** keep the union fallback for single-table scope so common
  queries are not over-rejected; expand test corpus before tightening.
- **OIDC rollout:** requires an IAM role + trust policy provisioned out-of-band; until then the
  pipeline cannot deploy. Sequence the CI gate (PR-only, no deploy) first so it is safe to land
  before the OIDC role exists.
