# Implementation Plan — Production Hardening

## Overview

Auth is intentionally out of scope. Tasks are ordered so the CI gate lands first and is
safe to merge before infra (OIDC, DLQ) exists. Each task references the requirements it
satisfies. Tasks 1–2 are foundational (CI gate + config plumbing); tasks 3–11 are largely
independent feature edits; task 12 is sequenced last because it depends on an externally
provisioned IAM/OIDC role; task 13 is final verification.

## Tasks

- [x] 1. Land the CI test/lint gate (no deploy changes yet)
  - Create `.github/workflows/ci.yml` triggered on `pull_request` and `push` to `main`.
  - Steps: checkout → setup Python 3.11 → `pip install -e .[dev]` → `ruff check .` → `ruff format --check .` → `pytest`.
  - Confirm a deliberately failing lint/test blocks the workflow.
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Add config plumbing for all new settings
  - Add the fields from the design "configuration summary" table to `Settings` in `config.py`, each with an explicit `Field(alias=...)` and the listed default.
  - Verify `get_settings()` still loads with an unchanged `.env` (defaults preserve current behavior).
  - _Requirements: 10.1, 10.3_

- [x] 3. Remove the production schema catalog from version control
  - [x] 3.1 Add `config/copilot_schema_catalog.json` to `.gitignore` and `git rm --cached` it (keep the file on disk).
  - [x] 3.2 Implement S3 fallback loading in `DatabaseCopilotService.catalog` (local file → `COPILOT_SCHEMA_CATALOG_S3_URI` → clear error), caching the S3-loaded catalog in memory.
  - [ ] 3.3 Document local vs. production catalog provisioning in `README.md`.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 4. Enforce read-only DB transactions and safe conninfo
  - [x] 4.1 Configure the pool with `autocommit=True` and assemble `conninfo` via `psycopg.conninfo.make_conninfo` in `PostgresCopilotExecutor`.
  - [x] 4.2 Manage the transaction explicitly: `BEGIN READ ONLY` → set `app.current_user_id` and `statement_timeout` (local) → `fetchmany(max_rows)` → `ROLLBACK`.
  - [ ] 4.3 Add a test asserting a write statement is rejected at the transaction level (fake connection recording statement order; optional live-PG integration test behind a marker).
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 5. Queue poison-message protection
  - [x] 5.1 Request `ApproximateReceiveCount` in `SqsIngestionQueue.receive` and carry `receive_count` on `ReceivedIngestionJob`.
  - [x] 5.2 Add `RagService.mark_failed(document_id, error)` helper for record persistence.
  - [x] 5.3 In `IngestionWorker._process_message`, abandon messages over `ingestion_max_receive_count`: delete, mark record `failed`, emit `rag_ingestion_jobs_abandoned_total`.
  - [ ] 5.4 Add worker tests: retry-on-failure under the limit; abandon-and-mark-failed over the limit.
  - [ ] 5.5 Document the recommended SQS redrive policy + DLQ (`maxReceiveCount` matching config) in `README.md`.
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 6. Dependency-aware health checks
  - [x] 6.1 Keep `/health` as immediate liveness; add `GET /ready` readiness endpoint in `api.py`.
  - [x] 6.2 Implement per-dependency probes (S3 `head_bucket`, Pinecone `describe_index_stats`, Bedrock client/config check, PostgreSQL `SELECT 1` when copilot enabled), each via `run_in_threadpool` with `asyncio.wait_for(readiness_probe_timeout_s)`.
  - [x] 6.3 Return 200 with a per-dependency map on success; 503 naming the failing dependency otherwise.
  - [ ] 6.4 Point container/compose `HEALTHCHECK` at `/health`; document `/ready` for orchestration readiness.
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

- [x] 7. Circuit breaker for external calls
  - [x] 7.1 Add `CircuitBreaker`, `CircuitOpenError`, and a `@circuit(name=...)` decorator (with provider registry) to `observability.py`, thread-safe with CLOSED/OPEN/HALF_OPEN states.
  - [x] 7.2 Wrap Bedrock and Pinecone call sites: `@circuit(...)` outermost, `@retry_on_transient()` inner, so an OPEN circuit fails fast without retry/backoff.
  - [x] 7.3 Map `CircuitOpenError` to HTTP 503 in `api.py` exception handling.
  - [x] 7.4 Emit `rag_circuit_state_total{provider,state}` metrics and log transitions.
  - [ ] 7.5 Tests: breaker opens after threshold, fails fast while open, half-opens after cooldown.
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 8. Generation context budget and fan-out metrics
  - [x] 8.1 In `BedrockNemotronGenerator.answer`, accumulate top-scoring chunk text up to `generation_max_context_chars`; drop the rest; ensure citations reflect only included chunks; log/observe dropped count.
  - [x] 8.2 Wire `generation_max_tokens` and `pinecone_upsert_batch_size` from `Settings` into `generation.py` and `retrieval.py`; wire `embedding_max_workers` into `embedding.py`.
  - [x] 8.3 Promote the Copilot SQL retry bound to `copilot_sql_max_attempts`; increment `rag_model_calls_total{path}` at each Bedrock call site and record `rag_ask_model_calls` on the hybrid path.
  - [ ] 8.4 Tests for context-budget truncation in `tests/test_generation.py`.
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 10.1_

- [x] 9. Table-scoped SQL column validation
  - [x] 9.1 Replace the union-based column check in `CopilotSqlGuard.validate` with table-scoped resolution using sqlglot qualify/scope; reject columns resolving to unapproved/unknown tables and ambiguous unqualified columns (with the single-table-scope fallback from the design).
  - [x] 9.2 Keep existing rejections (unapproved table, `SELECT *`, write/DDL, aggregate-required).
  - [ ] 9.3 Add tests in `tests/test_copilot.py`: cross-table column spoofing, CTEs, subqueries, `UNION`.
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [ ] 10. Storage and document-record tests
  - [ ] 10.1 Extend `tests/test_storage.py` with mocked-S3 put/get round-trip, SSE header selection, and `NoSuchKey → None`.
  - [ ] 10.2 Add a conflicting document-record update test to `tests/test_service_document_records.py`.
  - _Requirements: 9.1, 9.4_

- [x] 11. Container and compose hygiene
  - [x] 11.1 Remove the `version: '3.8'` key from `docker-compose.yml`; convert `api.depends_on` to `condition: service_healthy`; add a worker healthcheck based on the `/tmp/worker-healthy` file.
  - [ ] 11.2 Update README/steering with all new environment variables.
  - _Requirements: 10.2, 10.4_

- [ ] 12. Switch deploy pipeline to OIDC and add image scan (last; needs external IAM role)
  - [ ] 12.1 Add `permissions: id-token: write` and replace static-key auth in `deploy.yml` with `role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}` (OIDC).
  - [ ] 12.2 Run `ruff`/`pytest` as the first steps in the deploy job (or `needs:` the CI workflow) so deploy aborts on failure; keep `wait-for-service-stability: true`.
  - [ ] 12.3 Add a container image vulnerability scan (Trivy) after build; surface results.
  - [ ] 12.4 Document the required IAM role/trust policy and the rollback runbook (redeploy previous image tag / task-definition revision) in `README.md`.
  - _Requirements: 1.4, 1.5, 1.6_

- [ ] 13. Final verification
  - Run `ruff check .`, `ruff format --check .`, and `pytest`; fix any failures.
  - Confirm every requirement maps to at least one completed task and a passing test or CI assertion.
  - _Requirements: 9.5_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"], "description": "Foundational: CI lint/test gate and Settings config plumbing." },
    { "wave": 2, "tasks": ["3", "4", "5", "6", "7", "8", "9", "11"], "description": "Independent feature edits; each consumes Wave 1 config." },
    { "wave": 3, "tasks": ["10"], "description": "Storage and document-record tests (build on task 5 changes)." },
    { "wave": 4, "tasks": ["12"], "description": "Deploy pipeline OIDC + image scan; depends on task 1 and an external IAM/OIDC role." },
    { "wave": 5, "tasks": ["13"], "description": "Final verification across all tasks." }
  ]
}
```

```
1 (CI gate) ──┐
2 (config)  ──┼─→ 3 (catalog)
              ├─→ 4 (read-only DB)
              ├─→ 5 (queue poison) ──→ 10 (storage/record tests)
              ├─→ 6 (health/ready)
              ├─→ 7 (circuit breaker)
              ├─→ 8 (context budget/metrics)
              ├─→ 9 (SQL column scoping)
              └─→ 11 (container/compose hygiene)

12 (deploy OIDC + scan)  depends on: 1, and external IAM/OIDC role
13 (final verification)  depends on: all of 1–12
```

- Task 2 (config) should land before 3–11 since each consumes new `Settings` fields.
- Tasks 3–11 can otherwise proceed in parallel.
- Task 12 is intentionally last (external dependency); the CI gate (1) is independent of it.

## Notes

- Auth (inbound auth/authorization and deriving `user_id` from a principal) is deferred by
  project decision and excluded from this plan.
- Live-PostgreSQL tests (task 4.3) are gated behind a pytest marker; the default suite uses a
  fake connection asserting statement order so CI needs no database.
- No IaC lives in the repo; SQS DLQ/redrive and the OIDC role are provisioned externally and
  only documented here (tasks 5.5, 12.4).
- Defaults for all new settings preserve current behavior, so changes can ship incrementally
  without coordinated config rollouts.

