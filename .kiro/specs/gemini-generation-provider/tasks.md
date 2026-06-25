# Implementation Plan: Gemini Generation Provider

## Overview

This plan introduces a backend-agnostic generation provider abstraction in a new
`rag_system/llm_provider.py` module, implements `BedrockProvider`, `GeminiProvider`, a
`FallbackProvider` decorator, and a `get_generation_provider` factory, then refactors the four
existing generation call sites (`generation.py`, `router.py`, `copilot.py`) to obtain text
exclusively through the abstraction. Configuration, readiness probing, packaging, and tests are
updated to preserve all existing resilience, observability, and response-shape behavior. Each step
builds incrementally and ends wired into the running system; the Bedrock path stays behavior-
preserving so existing tests pass with `LLM_PROVIDER=bedrock`.

## Tasks

- [x] 1. Add provider configuration to Settings
  - [x] 1.1 Add new generation-provider fields to `Settings` in `config.py`
    - Add `llm_provider: Literal["bedrock","gemini"]` with `Field(default="bedrock", alias="LLM_PROVIDER")`
    - Add `gemini_model_id` (`GEMINI_MODEL_ID`, default `"gemini-1.5-pro"`), `gcp_project_id` (`GCP_PROJECT_ID`, default `None`), `gcp_location` (`GCP_LOCATION`, default `"us-central1"`)
    - Add `google_application_credentials` (`GOOGLE_APPLICATION_CREDENTIALS`, default `None`, `repr=False`), `llm_fallback_to_bedrock` (`LLM_FALLBACK_TO_BEDROCK`, default `False`), `gemini_read_timeout_s` (`GEMINI_READ_TIMEOUT_S`, default `90`)
    - Ensure invalid `LLM_PROVIDER` values are rejected at load via the `Literal`/field validator with a descriptive error; never read `os.environ` directly
    - _Requirements: 2.1, 2.2, 2.3, 2.6, 3.1, 3.2, 3.3, 3.5, 8.2, 6.3_

  - [ ]* 1.2 Write property test for invalid provider configuration rejection
    - **Property 8: Invalid provider configuration is rejected at load**
    - **Validates: Requirements 2.5**

  - [ ]* 1.3 Write unit tests for Settings fields and defaults
    - Test `LLM_PROVIDER` accepted values and bedrock default, `GEMINI_MODEL_ID` verbatim, `GCP_*` defaults, `repr=False` on credential field, fallback default false
    - Verify Secrets Manager delivery of `GOOGLE_APPLICATION_CREDENTIALS` via the existing loader (mocked)
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.5, 3.7, 8.2_

- [x] 2. Create the generation provider abstraction and data types
  - [x] 2.1 Define `GenerationRequest`, `GenerationResult`, and the `GenerationProvider` protocol
    - Create `src/rag_system/llm_provider.py`
    - Add frozen dataclasses `GenerationRequest` (user_prompt, optional system_prompt, temperature, max_output_tokens) and `GenerationResult` (text, usage dict)
    - Define the `GenerationProvider` Protocol with `name`, `generate(request)`, and `readiness_check()`
    - _Requirements: 1.1, 1.4, 1.5_

  - [ ]* 2.2 Write unit tests for the provider data types and protocol contract
    - Test dataclass defaults/immutability and protocol shape
    - _Requirements: 1.1, 1.4, 1.5_

- [x] 3. Implement `BedrockProvider`
  - [x] 3.1 Implement `BedrockProvider` in `llm_provider.py`
    - Construct `bedrock-runtime` client via `settings.boto3_session()` and `settings.bedrock_botocore_config()`
    - Implement `_generate_inner` (decorated with `@retry_on_transient()`) calling Converse with mapped temperature, maxTokens, optional `system`, returning `GenerationResult(text, usage)`
    - Implement circuit-protected `generate()` using `get_circuit_breaker("bedrock", ...)` (move the pattern out of `generation.py`)
    - Implement `readiness_check()` that constructs the client only (no model call)
    - _Requirements: 1.2, 6.1, 6.2, 6.3, 8.1, 9.2_

  - [ ]* 3.2 Write unit tests for `BedrockProvider` with a mocked bedrock-runtime client
    - Verify Converse kwargs mapping, usage return, and readiness client construction without model calls
    - _Requirements: 1.2, 8.1, 9.2_

- [x] 4. Implement `GeminiProvider`
  - [x] 4.1 Implement `GeminiProvider` in `llm_provider.py`
    - Lazy `import vertexai`; re-raise `ImportError` as a descriptive `RuntimeError` instructing install of `google-cloud-aiplatform`
    - Raise descriptive config error when provider is gemini and `gcp_project_id` is missing; honor `GOOGLE_APPLICATION_CREDENTIALS`/ADC via `vertexai.init(project, location)`
    - Implement `_build_model` (system_instruction from system_prompt only when present) and `_generate_inner` decorated with `@retry_on_transient(retryable_exceptions=_GEMINI_TRANSIENT_ERRORS)`, mapping temperature, max_output_tokens, and per-call `request_options={"timeout": gemini_read_timeout_s}`
    - Add `_normalize_usage` mapping Vertex `usage_metadata` to `{"inputTokens","outputTokens","totalTokens"}`
    - Implement circuit-protected `generate()` using `get_circuit_breaker("gemini", ...)` and `readiness_check()` that validates import/project/init without a model call
    - Define `_GEMINI_TRANSIENT_ERRORS` (ServiceUnavailable, TooManyRequests/ResourceExhausted, DeadlineExceeded, InternalServerError, ConnectionError, TimeoutError)
    - _Requirements: 1.3, 3.4, 3.6, 4.1, 4.2, 4.3, 4.4, 4.5, 6.1, 6.2, 6.3, 6.4, 7.1, 7.4, 7.5, 9.1, 10.4_

  - [ ]* 4.2 Write property test for Gemini request-to-Vertex mapping
    - **Property 1: Gemini request-to-Vertex mapping**
    - **Validates: Requirements 1.4, 4.1, 4.2, 4.3, 4.4, 4.5**

  - [ ]* 4.3 Write property test for transient failure retry and circuit opening
    - **Property 5: Transient failures are retried and open the circuit**
    - **Validates: Requirements 6.1, 6.2, 7.1, 7.2, 7.5, 11.3**

  - [ ]* 4.4 Write property test for non-transient failure propagation
    - **Property 6: Non-transient failures are not retried**
    - **Validates: Requirements 7.4**

  - [ ]* 4.5 Write unit tests for GeminiProvider config/import/readiness errors
    - Test missing-project error (3.6), Vertex import failure descriptive error (10.4), readiness without model calls (9.1), read-timeout value passed (6.3)
    - _Requirements: 3.4, 3.6, 6.3, 9.1, 10.4_

- [x] 5. Implement structured logging and token-usage metric emission in providers
  - [x] 5.1 Emit `rag_generation_tokens` and structured logs from both providers
    - On successful generation, record token usage to `rag_generation_tokens` labelled `model_id` + `token_type`, and log entries including active `model_id` and `get_trace_id()`
    - Keep the exact metric name and label/field names used by the Bedrock path for dashboard parity
    - _Requirements: 6.4, 6.5, 6.6_

  - [ ]* 5.2 Write property test for token usage reporting on success
    - **Property 2: Successful generation always reports token usage**
    - **Validates: Requirements 1.5, 6.4**

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement `FallbackProvider` and the provider factory
  - [x] 7.1 Implement `FallbackProvider` decorator in `llm_provider.py`
    - On primary `CircuitOpenError` or transient error, re-raise (do not mask); on non-transient failure, increment `rag_generation_provider_fallback_total` and emit a structured warning log, then call the secondary
    - `readiness_check()` delegates to the primary
    - _Requirements: 8.3, 8.4, 8.5_

  - [x] 7.2 Implement `get_generation_provider(settings)` factory
    - Return `GeminiProvider` when `llm_provider == "gemini"`, wrapped in `FallbackProvider(primary, BedrockProvider(settings))` when `llm_fallback_to_bedrock` is true; otherwise return `BedrockProvider`
    - _Requirements: 2.4_

  - [ ]* 7.3 Write property test for fallback firing only when enabled
    - **Property 7: Fallback fires only when enabled**
    - **Validates: Requirements 8.3, 8.4, 8.5**

  - [ ]* 7.4 Write unit test for factory provider selection
    - Verify factory returns Gemini for `gemini`, Bedrock for `bedrock`, and the fallback wrapper when enabled
    - _Requirements: 2.4, 11.2_

- [x] 8. Refactor RAG answer generation call site
  - [x] 8.1 Refactor `generation.py` to use the provider abstraction
    - Rename `BedrockNemotronGenerator` to `AnswerGenerator` keeping a backward-compatible alias; accept an injected `GenerationProvider`
    - Replace the inline Converse + circuit logic with `provider.generate(GenerationRequest(user_prompt=prompt, temperature=0.1, max_output_tokens=max_tokens))`; emit `rag_generation_tokens` from `result.usage` as today; leave citation construction and `evidence_status` untouched
    - _Requirements: 1.6, 5.1, 5.4, 5.5_

  - [ ]* 8.2 Write property test for grounding and citations depending only on evidence
    - **Property 4: Grounding and citations depend only on evidence**
    - **Validates: Requirements 5.4, 5.5**

- [ ] 9. Refactor router call sites
  - [x] 9.1 Refactor `BedrockQueryClassifier.classify` and `AgenticRouter._synthesize_hybrid` in `router.py`
    - Classifier: `GenerationRequest(user_prompt=prompt, temperature=0.0, max_output_tokens=256)`, parse routing JSON via existing `_parse_routing_response`/fallback
    - Hybrid synthesis: `GenerationRequest(user_prompt=prompt, temperature=0.1, max_output_tokens=4096)`
    - Inject the `GenerationProvider`; remove direct Converse usage
    - _Requirements: 1.6, 5.2_

  - [ ]* 9.2 Write unit tests for classifier JSON parsing through a mocked provider
    - Verify `RoutingDecision` parsing and fallback behavior using a mocked provider
    - _Requirements: 5.2_

- [x] 10. Refactor Copilot call site
  - [x] 10.1 Refactor `BedrockDatabaseCopilot._call_bedrock` in `copilot.py`
    - Replace with `provider.generate(GenerationRequest(user_prompt=user_prompt, system_prompt=system_prompt, temperature=0.0, max_output_tokens=2048))` for intent/table/SQL/answer generation
    - Inject the `GenerationProvider`; keep `CopilotQueryResponse` shape unchanged
    - _Requirements: 1.4, 1.6, 5.3_

  - [ ]* 10.2 Write unit tests for Copilot generation through a mocked provider
    - Verify system/user prompt split is passed through and response shape preserved
    - _Requirements: 1.4, 5.3_

- [x] 11. Wire providers and readiness probe into the API layer
  - [x] 11.1 Add a cached provider factory and inject it into call-site factories in `api.py`
    - Add a `get_generation_provider`-backed `@lru_cache` factory and pass the provider into `get_service`, `get_router`, and `get_copilot_service`
    - _Requirements: 1.6, 2.4_

  - [x] 11.2 Replace `probe_bedrock` with `probe_generation_provider` in the `/ready` endpoint
    - Resolve the active provider via the factory and call `readiness_check()` under `readiness_probe_timeout_s`; on failure report dependency key `generation` and return 503
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [ ]* 11.3 Write unit tests for readiness probe behavior
    - Test gemini/bedrock probe without model calls, failure → 503 identifying `generation`, and `CircuitOpenError` → 503 surfacing
    - _Requirements: 7.3, 9.1, 9.2, 9.3, 9.4_

- [x] 12. Declare dependency and document configuration
  - [x] 12.1 Add the Vertex AI dependency and document env vars
    - Add `google-cloud-aiplatform>=1.60.0` to `pyproject.toml` dependencies
    - Document `LLM_PROVIDER`, `GEMINI_MODEL_ID`, `GCP_PROJECT_ID`, `GCP_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`, `LLM_FALLBACK_TO_BEDROCK` (and `GEMINI_READ_TIMEOUT_S`) with placeholder values in `.env.example`
    - Confirm the `Dockerfile` installs the project so the new dependency is importable at runtime
    - _Requirements: 10.1, 10.2, 10.3_

  - [ ]* 12.2 Write packaging/config smoke tests
    - Assert `pyproject.toml` pins `google-cloud-aiplatform` minimum and `.env.example` documents the new keys
    - _Requirements: 10.1, 10.2_

- [ ] 13. Cross-provider parity tests
  - [ ]* 13.1 Write property test for response-shape parity across providers
    - **Property 3: Response-shape parity across providers**
    - **Validates: Requirements 5.1, 5.2, 5.3, 8.1, 11.4**

  - [ ]* 13.2 Write integration tests exercising all four call sites with a mocked provider
    - Ensure no external AWS or GCP calls occur; confirm existing Bedrock-path tests still pass with `LLM_PROVIDER=bedrock`
    - _Requirements: 11.1, 8.1_

- [x] 14. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP.
- Each task references specific requirements for traceability.
- Checkpoints ensure incremental validation.
- Property tests (Hypothesis, min 100 iterations) validate the universal correctness properties from the design; unit/integration/smoke tests cover configuration, packaging, and wiring.
- All provider calls are mocked in tests so no external AWS or GCP calls occur.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "12.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2", "3.1", "4.1", "12.2"] },
    { "id": 2, "tasks": ["3.2", "4.2", "4.3", "4.4", "4.5", "5.1", "7.1", "7.2"] },
    { "id": 3, "tasks": ["5.2", "7.3", "7.4", "8.1", "9.1", "10.1"] },
    { "id": 4, "tasks": ["8.2", "9.2", "10.2", "11.1"] },
    { "id": 5, "tasks": ["11.2"] },
    { "id": 6, "tasks": ["11.3", "13.1", "13.2"] }
  ]
}
```
