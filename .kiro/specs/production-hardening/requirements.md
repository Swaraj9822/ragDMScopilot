# Requirements Document

## Introduction

This spec addresses the production-readiness concerns surfaced by the codebase audit,
**excluding authentication/authorization**, which is intentionally deferred to a later
stage by project decision. The goal is to close the gaps that make the system unsafe or
expensive to operate while it matures: deployment safety, secret/schema exposure,
database read-only enforcement, queue poison handling, dependency-aware health,
resilience under downstream failure, request cost/latency control, and the test gate that
protects all of it.

Scope is limited to the existing FastAPI + worker application (`src/rag_system/`), its
CI/CD pipeline (`.github/workflows/deploy.yml`), and container/compose configuration.
No architectural rewrite is intended; every change is incremental on the current design.

### Out of scope
- Inbound authentication and authorization (deferred by design).
- Deriving `user_id` from an authenticated principal (depends on auth).
- New product features (chat UI, streaming responses, namespaces).

## Glossary

- **Copilot**: the NL→SQL service (`DatabaseCopilotService`) that answers questions over PostgreSQL.
- **Catalog**: `copilot_schema_catalog.json`, the allow-listed table/column schema the Copilot may query.
- **SQL guard**: `CopilotSqlGuard`, the sqlglot-based validator enforcing read-only/aggregate/allow-list rules.
- **Readiness vs liveness**: liveness = process is up; readiness = critical dependencies are reachable.
- **Circuit breaker**: a fail-fast wrapper that stops calling a failing downstream after a failure threshold.
- **DLQ**: SQS dead-letter queue; **redrive policy**: SQS config that moves messages to the DLQ after `maxReceiveCount` receives.

---

## Requirements

### Requirement 1: CI/CD safety gate

**User Story:** As an operator, I want the pipeline to block bad code before it reaches
production, so that a regression cannot be deployed unnoticed.

#### Acceptance Criteria
1. WHEN a pull request targets `main`, THE pipeline SHALL run `ruff check`, `ruff format --check`, and `pytest` and report status.
2. WHEN any lint or test step fails, THE pipeline SHALL NOT build or deploy the image.
3. WHEN code is pushed to `main`, THE pipeline SHALL run the same lint and test steps before the build and deploy steps execute.
4. THE pipeline SHALL build the container image and scan it for known vulnerabilities, and SHALL surface scan results as a job artifact or log.
5. WHERE AWS credentials are required, THE pipeline SHALL authenticate using GitHub OIDC role assumption rather than long-lived static IAM access keys.
6. WHEN a deployment to ECS does not reach service stability, THE pipeline SHALL fail the job so the failure is visible.

### Requirement 2: Remove production schema/secret exposure from version control

**User Story:** As a security-conscious engineer, I want the production database schema
catalog out of the git repository, so that cloning the repo does not disclose the ERP
data model.

#### Acceptance Criteria
1. THE repository SHALL NOT track `config/copilot_schema_catalog.json`; it SHALL be listed in `.gitignore`.
2. THE repository SHALL retain a sanitized `config/copilot_schema_catalog.example.json` showing structure without production table/column specifics.
3. WHEN the application starts and a local catalog file is absent, THE Copilot SHALL load the catalog from a configured source (local path or S3 object) determined by settings.
4. WHEN the catalog is loaded from S3, THE source location SHALL be provided via a `Settings` field with an explicit env alias.
5. THE README SHALL document how to provide the catalog in local development and in production.

### Requirement 3: Enforce read-only database access in the Copilot executor

**User Story:** As a data owner, I want every Copilot query to run in a guaranteed
read-only transaction, so that no generated SQL can mutate the database even if the
guard is bypassed.

#### Acceptance Criteria
1. THE Copilot connection pool SHALL be configured so that the read-only transaction directive is actually applied to each connection (e.g. autocommit connections with an explicit `BEGIN READ ONLY`, or a connection-level `default_transaction_read_only`).
2. WHEN a generated SQL statement attempts a write while the read-only setting is active, THE database SHALL reject it and THE service SHALL surface the error rather than committing.
3. THE statement timeout SHALL continue to be applied per query.
4. THE connection string SHALL be assembled with `psycopg.conninfo.make_conninfo` (or keyword connection parameters) rather than f-string interpolation, so that values containing spaces or special characters are handled correctly.
5. THERE SHALL be a test that confirms a write statement is rejected at the transaction level (independent of the SQL guard).

### Requirement 4: Queue poison-message and retry-loop protection

**User Story:** As an operator, I want documents that repeatedly fail ingestion to stop
being retried forever, so that a single bad document cannot burn parsing/LLM cost
indefinitely.

#### Acceptance Criteria
1. WHEN an ingestion job fails processing, THE worker SHALL allow SQS redrive to a dead-letter queue after a bounded number of receives.
2. WHERE the SQS redrive policy cannot be relied upon, THE worker SHALL read the message receive count and, WHEN it exceeds a configured maximum, SHALL stop retrying that message and record it as failed.
3. WHEN a message is abandoned after exceeding the retry limit, THE worker SHALL persist the document record as `failed` with the error and emit a failure metric.
4. THE maximum receive/retry count SHALL be configurable via `Settings`.
5. THE infrastructure expectations (queue + DLQ + redrive policy) SHALL be documented for the operator, even if provisioning is external.

### Requirement 5: Dependency-aware health checks

**User Story:** As an operator, I want health checks that reflect real dependency
status, so that an unhealthy deployment is not reported as healthy.

#### Acceptance Criteria
1. THE API SHALL expose a liveness check that returns 200 when the process is running.
2. THE API SHALL expose a readiness check that verifies connectivity to its critical dependencies (S3, Pinecone, Bedrock, and PostgreSQL when the Copilot is enabled).
3. WHEN any critical dependency check fails, THE readiness check SHALL return a non-200 status and identify which dependency failed.
4. THE readiness check SHALL apply a short per-dependency timeout so the check itself cannot hang.
5. THE container and compose healthchecks SHALL target the liveness endpoint, and orchestration readiness SHALL use the readiness endpoint.

### Requirement 6: Circuit breaking on external calls

**User Story:** As an operator, I want the system to fail fast when a downstream
provider is down, so that an outage does not amplify into a retry storm.

#### Acceptance Criteria
1. WHEN consecutive failures against an external provider (Bedrock, Pinecone) exceed a configured threshold within a time window, THE system SHALL open a circuit and fail fast for subsequent calls.
2. WHEN the circuit is open and a cooldown period elapses, THE system SHALL allow a trial call to determine whether to close the circuit.
3. WHEN the circuit is open, THE affected endpoint SHALL return a clear error (HTTP 503) instead of retrying with backoff.
4. THE circuit-breaker thresholds and cooldown SHALL be configurable via `Settings`.
5. THE circuit state transitions SHALL emit metrics and structured logs.

### Requirement 7: Request cost and latency control

**User Story:** As an operator, I want the `/ask` path to avoid unbounded LLM fan-out and
oversized prompts, so that latency and Bedrock cost stay predictable.

#### Acceptance Criteria
1. THE generation prompt SHALL enforce a configurable context-size budget; WHEN retrieved context exceeds the budget, THE service SHALL truncate or drop lowest-scoring chunks before calling the model.
2. THE Copilot SQL-generation retry loop SHALL remain bounded by a configurable maximum attempt count.
3. THE number of sequential model calls on the hybrid `/ask` path SHALL be measured and exposed as a metric.
4. WHERE a model call result is deterministic for a given input within a request, THE system SHALL avoid issuing it more than once.
5. THE per-endpoint request timeouts SHALL remain in force and configurable.

### Requirement 8: SQL guard column scoping

**User Story:** As a data owner, I want column validation to be scoped to the table a
column belongs to, so that the allow-list cannot be satisfied by a column from an
unrelated approved table.

#### Acceptance Criteria
1. WHEN a query references a column, THE guard SHALL validate that column against the columns of the table it is actually selected from, not the union of all referenced tables.
2. WHERE a column reference is unqualified and ambiguous across referenced tables, THE guard SHALL reject the query with a clear validation error.
3. THE guard SHALL continue to reject unapproved tables, `SELECT *`, write/DDL statements, and non-aggregated queries.
4. THERE SHALL be tests covering cross-table column spoofing, CTEs, subqueries, and `UNION`.

### Requirement 9: Test coverage for high-risk, low-coverage modules

**User Story:** As a maintainer, I want the thinly tested modules covered, so that the
hardening changes are protected against regression.

#### Acceptance Criteria
1. THERE SHALL be tests for `S3ArtifactStore` operations (put/get/round-trip and not-found handling) using a mocked S3 client.
2. THERE SHALL be tests for the generation grounding path (citation building and evidence status), with the Bedrock client mocked.
3. THERE SHALL be tests for the worker failure paths (parser/chunker/embedder failure and retry-limit abandonment).
4. THERE SHALL be a test for concurrent or conflicting document-record updates at the storage layer.
5. ALL new and existing tests SHALL pass under `pytest`, and `ruff check` SHALL report no errors.

### Requirement 10: Configuration and container hygiene

**User Story:** As a maintainer, I want operational knobs in config and the container
definitions current, so that tuning does not require code edits and compose does not warn.

#### Acceptance Criteria
1. THE hardcoded operational values that affect cost/latency (Pinecone upsert batch size, retrieval top-k already in config, generation `maxTokens`, embedding worker pool size) SHALL be exposed through `Settings` with explicit env aliases.
2. THE `docker-compose.yml` SHALL not rely on the obsolete top-level `version` key, and `depends_on` SHALL express a health-based condition where supported.
3. THE new `Settings` fields SHALL have safe defaults matching current behavior so existing deployments are unaffected.
4. THE README/steering SHALL list the new environment variables.
