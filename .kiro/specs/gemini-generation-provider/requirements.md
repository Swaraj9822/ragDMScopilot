# Requirements Document

## Introduction

This feature substitutes the AWS Bedrock generation model (currently `nvidia.nemotron-super-3-120b`,
accessed through the Bedrock Converse API) with Google Gemini served through Google Cloud Platform's
Vertex AI. The substitution is achieved by introducing a backend-agnostic generation provider
abstraction so the four existing generation call sites can run against either Bedrock or Gemini,
selected by configuration.

**Scope** is limited to the **generation (text completion / chat) model only**. The embedding model
(`amazon.titan-embed-text-v2:0`) and all chunking embeddings remain on AWS Bedrock and are explicitly
**out of scope**. All other AWS usage (S3, SQS, Secrets Manager, CloudWatch, Pinecone, PostgreSQL)
is unchanged.

The four generation call sites in scope are:

1. `BedrockNemotronGenerator.answer` (`generation.py`) — grounded RAG answer generation with citations.
2. `BedrockQueryClassifier.classify` (`router.py`) — JSON routing-decision classification.
3. `AgenticRouter._synthesize_hybrid` (`router.py`) — hybrid RAG + database answer synthesis.
4. `BedrockDatabaseCopilot._call_bedrock` (`copilot.py`) — Copilot intent check, table selection,
   SQL generation, and answer generation (uses a separate system prompt).

The feature must preserve existing cross-cutting behavior: `@retry_on_transient()` retries, the
shared circuit breaker, per-call read timeouts, token/usage metrics (`rag_generation_tokens`),
structured logging with trace IDs, response shapes, `evidence_status` semantics, and citation
behavior.

## Glossary

- **Generation_Provider**: The backend-agnostic abstraction (interface/protocol) for invoking a
  large language model to produce text. Implemented by `Bedrock_Provider` and `Gemini_Provider`.
- **Bedrock_Provider**: The Generation_Provider implementation that calls the AWS Bedrock Converse API.
- **Gemini_Provider**: The Generation_Provider implementation that calls Google Gemini via GCP Vertex AI.
- **Provider_Factory**: The component that constructs the configured Generation_Provider instance
  based on `Settings`.
- **Settings**: The `rag_system.config.Settings` pydantic-settings object, the single source of
  configuration, populated from environment variables / `.env` / AWS Secrets Manager.
- **Generation_Request**: The provider-agnostic input to a Generation_Provider, consisting of a user
  prompt, an optional system prompt, and inference parameters (temperature, max output tokens).
- **Generation_Result**: The provider-agnostic output of a Generation_Provider, consisting of the
  generated text and a usage dictionary (token counts).
- **LLM_Provider_Setting**: The configuration value (`LLM_PROVIDER`) that selects which
  Generation_Provider is active (`bedrock` or `gemini`).
- **Vertex_AI**: Google Cloud Vertex AI, the managed service exposing Gemini models.
- **GCP_Credentials**: The Google service-account credentials (Application Default Credentials or a
  JSON key) used to authenticate to Vertex_AI.
- **Circuit_Breaker**: The shared fail-fast wrapper from `observability.py`
  (`get_circuit_breaker`), keyed by a provider name.
- **Readiness_Check**: The `GET /ready` endpoint in `api.py` that probes critical dependency
  connectivity.
- **Transient_Error**: An error class that the retry/circuit-breaker layer treats as retryable
  (e.g. connection errors, timeouts, throttling / rate-limit, temporary upstream unavailability).
- **Evidence_Status**: The grounding label on a response (`grounded`, `partially_grounded`,
  `insufficient_evidence`, `no_rows`).

## Requirements

### Requirement 1: Backend-agnostic generation provider abstraction

**User Story:** As a developer, I want a single generation provider abstraction, so that all
generation call sites are independent of whether the backend is Bedrock or Gemini.

#### Acceptance Criteria

1. THE Generation_Provider SHALL expose a single text-generation operation that accepts a
   Generation_Request (user prompt, optional system prompt, temperature, and maximum output tokens)
   and returns a Generation_Result (generated text and a usage dictionary).
2. THE Bedrock_Provider SHALL implement the Generation_Provider operation using the AWS Bedrock
   Converse API.
3. THE Gemini_Provider SHALL implement the Generation_Provider operation using Google Gemini through
   GCP Vertex_AI.
4. WHERE a call site requires a system prompt, THE Generation_Provider SHALL accept the system prompt
   as a distinct field of the Generation_Request and apply it to the underlying model invocation.
5. THE Generation_Provider SHALL return token usage counts in the Generation_Result for every
   successful generation.
6. THE four generation call sites (RAG answer generation, query classification, hybrid synthesis, and
   Copilot intent/table/SQL/answer generation) SHALL obtain generated text exclusively through the
   Generation_Provider abstraction rather than calling a backend SDK directly.

### Requirement 2: Provider selection via configuration

**User Story:** As an operator, I want to select the generation backend through configuration, so
that I can switch between Bedrock and Gemini without code changes.

#### Acceptance Criteria

1. THE Settings SHALL define an LLM_Provider_Setting field with alias `LLM_PROVIDER` that accepts the
   values `bedrock` and `gemini`.
2. WHERE the LLM_Provider_Setting is not supplied, THE Settings SHALL default the LLM_Provider_Setting
   to `bedrock`.
3. THE Settings SHALL define a Gemini model identifier field with alias `GEMINI_MODEL_ID` whose value
   is used verbatim as the Vertex_AI model name.
4. WHEN the Provider_Factory constructs a Generation_Provider, THE Provider_Factory SHALL return a
   Gemini_Provider WHILE the LLM_Provider_Setting equals `gemini`, and a Bedrock_Provider WHILE the
   LLM_Provider_Setting equals `bedrock`.
5. IF the LLM_Provider_Setting holds a value other than `bedrock` or `gemini`, THEN THE Settings SHALL
   reject the configuration with a descriptive error at load time.
6. THE Settings SHALL define all new configuration fields using `Field(..., alias="ENV_NAME")` and
   SHALL NOT read environment variables directly outside the Settings object.

### Requirement 3: GCP authentication and project configuration

**User Story:** As an operator, I want to provide GCP credentials, project, and location through
configuration, so that the Gemini_Provider can authenticate to Vertex_AI.

#### Acceptance Criteria

1. THE Settings SHALL define a GCP project identifier field with alias `GCP_PROJECT_ID`.
2. THE Settings SHALL define a Vertex_AI location/region field with alias `GCP_LOCATION` whose default
   is a documented Vertex_AI region.
3. THE Settings SHALL define a GCP service-account credentials path field with alias
   `GOOGLE_APPLICATION_CREDENTIALS` that identifies the service-account JSON key file location.
4. WHERE the GCP service-account credentials path is not supplied, THE Gemini_Provider SHALL
   authenticate using Application Default Credentials.
5. THE Settings SHALL mark any field that holds raw credential material with `repr=False`.
6. WHEN the LLM_Provider_Setting equals `gemini` AND the GCP project identifier is absent, THEN THE
   Gemini_Provider SHALL raise a descriptive configuration error identifying the missing setting.
7. WHERE the GCP service-account credentials are supplied through AWS Secrets Manager, THE Settings
   SHALL load them through the existing Secrets Manager mechanism (`SECRETS_MANAGER_SECRET_ID`)
   without reading environment variables directly.

### Requirement 4: Inference parameter and prompt mapping

**User Story:** As a developer, I want inference parameters and prompts mapped correctly onto the
Vertex_AI API, so that Gemini produces equivalent outputs to the Bedrock call sites.

#### Acceptance Criteria

1. WHEN a Generation_Request specifies a temperature, THE Gemini_Provider SHALL pass that temperature
   to the Vertex_AI generation configuration.
2. WHEN a Generation_Request specifies a maximum output token count, THE Gemini_Provider SHALL pass
   that value as the Vertex_AI maximum output token configuration.
3. WHEN a Generation_Request includes a system prompt, THE Gemini_Provider SHALL supply that system
   prompt to Vertex_AI through the system-instruction mechanism, separate from the user prompt.
4. WHEN a Generation_Request omits a system prompt, THE Gemini_Provider SHALL invoke Vertex_AI with
   only the user prompt.
5. THE Gemini_Provider SHALL return the generated text as a plain string equivalent in shape to the
   text returned by the Bedrock_Provider.

### Requirement 5: Behavior and response-shape parity

**User Story:** As a consumer of the API, I want responses to keep the same shapes and grounding
semantics regardless of provider, so that switching providers does not break clients.

#### Acceptance Criteria

1. WHEN generation runs through the Gemini_Provider, THE RAG answer path SHALL produce a
   `QueryResponse` with the same fields (answer, citations, evidence_status, trace_id) as the Bedrock
   path.
2. WHEN generation runs through the Gemini_Provider, THE query classifier SHALL produce a
   `RoutingDecision` parsed from the model's JSON output using the existing parsing and fallback logic.
3. WHEN generation runs through the Gemini_Provider, THE Copilot path SHALL produce a
   `CopilotQueryResponse` with the same fields (answer, mode, evidence_status, trace_id, sql, rows,
   data_sources) as the Bedrock path.
4. THE Evidence_Status assigned to a response SHALL depend only on retrieved evidence and query
   results and SHALL NOT depend on which Generation_Provider produced the text.
5. THE citation construction for the RAG answer path SHALL be identical regardless of which
   Generation_Provider produced the text.

### Requirement 6: Preserve resilience, timeout, and observability behavior

**User Story:** As an operator, I want retries, circuit breaking, timeouts, metrics, and logging to
keep working with Gemini, so that production resilience and visibility are unchanged.

#### Acceptance Criteria

1. THE Gemini_Provider SHALL apply the existing `@retry_on_transient()` behavior to its Vertex_AI
   calls.
2. THE Gemini_Provider SHALL route its calls through a Circuit_Breaker so that repeated failures open
   the circuit and cause subsequent calls to fail fast.
3. THE Gemini_Provider SHALL enforce a per-call read timeout bounded by a configurable Settings value.
4. WHEN a Gemini generation completes successfully, THE Gemini_Provider SHALL record token usage to
   the `rag_generation_tokens` metric labelled with the active model identifier and token type.
5. THE Gemini_Provider SHALL emit structured log entries that include the active model identifier and
   the current trace ID for each generation call.
6. THE metrics and log fields emitted for generation SHALL retain the same metric names and field
   names used by the Bedrock path so that existing dashboards continue to function.

### Requirement 7: Transient error classification and HTTP surfacing

**User Story:** As an operator, I want Gemini transient failures handled like Bedrock failures, so
that error responses and recovery behavior stay consistent.

#### Acceptance Criteria

1. WHEN a Vertex_AI call raises a Transient_Error (connection failure, timeout, throttling /
   rate-limit, or temporary unavailability), THE Gemini_Provider SHALL classify that error as
   retryable so the retry layer retries it.
2. IF Gemini failures cause the Circuit_Breaker to open, THEN THE system SHALL raise the existing
   `CircuitOpenError` for subsequent calls.
3. WHEN a `CircuitOpenError` is raised during a request, THE API SHALL return HTTP status 503
   irrespective of any other status code set earlier in the request pipeline.
4. IF a Vertex_AI call raises a non-transient error, THEN THE Gemini_Provider SHALL propagate the
   error without retrying it.
5. WHEN a Gemini generation call exceeds its configured read timeout, THE Gemini_Provider SHALL treat
   the timeout as a Transient_Error.

### Requirement 8: Selectable Bedrock provider and optional fallback

**User Story:** As an operator, I want to keep Bedrock available as a provider and optionally fall
back to it, so that I can mitigate risk during the migration.

#### Acceptance Criteria

1. WHILE the LLM_Provider_Setting equals `bedrock`, THE system SHALL perform all generation through
   the Bedrock_Provider with behavior identical to the pre-existing Bedrock implementation.
2. THE Settings SHALL define a boolean fallback-enable field with alias `LLM_FALLBACK_TO_BEDROCK`
   whose default is `false`.
3. WHILE the LLM_Provider_Setting equals `gemini` AND the fallback-enable field is `true`, IF a
   Gemini generation call fails with a non-transient error after retries are exhausted, THEN THE
   system SHALL attempt the same Generation_Request through the Bedrock_Provider.
4. WHEN a fallback to the Bedrock_Provider occurs, THE system SHALL emit a metric and a structured log
   entry recording that a provider fallback happened.
5. WHILE the fallback-enable field is `false`, IF a Gemini generation call fails after retries are
   exhausted, THEN THE system SHALL propagate the failure without invoking the Bedrock_Provider.

### Requirement 9: Readiness probe for the active generation provider

**User Story:** As an operator, I want the readiness check to validate the active generation provider,
so that orchestration does not route traffic to an instance that cannot generate answers.

#### Acceptance Criteria

1. WHILE the LLM_Provider_Setting equals `gemini`, THE Readiness_Check SHALL probe the Gemini_Provider
   by verifying client construction and credential/project configuration without invoking the model.
2. WHILE the LLM_Provider_Setting equals `bedrock`, THE Readiness_Check SHALL probe the Bedrock_Provider
   as it currently does.
3. IF the active generation provider probe fails, THEN THE Readiness_Check SHALL return HTTP status 503
   and identify the generation provider as the failing dependency.
4. THE Readiness_Check SHALL apply the existing per-dependency probe timeout to the generation provider
   probe.

### Requirement 10: Dependencies and packaging

**User Story:** As a developer, I want the Vertex_AI client dependency declared and packaged, so that
the Gemini_Provider can run in local, container, and CI environments.

#### Acceptance Criteria

1. THE project dependency manifest (`pyproject.toml`) SHALL declare the Google Vertex_AI / Gemini
   client library required by the Gemini_Provider with a pinned minimum version.
2. THE example environment file (`.env.example`) SHALL document the new configuration fields
   (`LLM_PROVIDER`, `GEMINI_MODEL_ID`, `GCP_PROJECT_ID`, `GCP_LOCATION`,
   `GOOGLE_APPLICATION_CREDENTIALS`, `LLM_FALLBACK_TO_BEDROCK`) with placeholder values.
3. THE container image build SHALL include the Vertex_AI client dependency so the Gemini_Provider is
   importable at runtime.
4. WHERE the Gemini_Provider is selected but its client library is not importable, THE Gemini_Provider
   SHALL raise a descriptive error instructing the operator to install the dependency.

### Requirement 11: Provider-agnostic tests

**User Story:** As a developer, I want tests that exercise both providers through mocks, so that
provider behavior is verified without calling external services.

#### Acceptance Criteria

1. THE test suite SHALL exercise each of the four generation call sites against the Generation_Provider
   abstraction using a mocked provider so that no external AWS or GCP calls occur during tests.
2. THE test suite SHALL verify that the Provider_Factory returns the Gemini_Provider WHEN the
   LLM_Provider_Setting equals `gemini` and the Bedrock_Provider WHEN it equals `bedrock`.
3. THE test suite SHALL verify that a Transient_Error from a mocked Gemini call triggers retry behavior
   and that exhausted retries surface through the Circuit_Breaker.
4. THE test suite SHALL verify that response shapes (`QueryResponse`, `RoutingDecision`,
   `CopilotQueryResponse`) are identical across the Bedrock_Provider and Gemini_Provider for the same
   inputs.
