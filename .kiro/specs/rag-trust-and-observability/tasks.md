# Implementation Plan: RAG Trust and Observability

## Overview

This plan implements the trust, evaluation, and observability enhancements described in the design as an additive layer over the existing Python backend (`src/rag_system`) and React + TypeScript frontend (`frontendkimchi/src`). Work proceeds foundation-first: shared data models, storage key functions, config, and TypeScript types are established, then each requirement area is built as pure logic → API wiring → frontend, with property-based tests (Hypothesis) placed next to the logic they validate and frontend tests (Vitest + Testing Library + MSW) next to each component.

Per the project testing preference, tests are always included: backend tests live under `tests/` (pytest/Hypothesis), frontend tests are colocated `*.test.ts(x)` (Vitest + Testing Library + MSW). Each of the 38 correctness properties is implemented as a single property test carrying a tag comment in the format `# Feature: rag-trust-and-observability, Property {n}: {property_text}`. The relevant suite must run and pass before a task is reported complete.

## Tasks

- [x] 1. Establish shared foundations (models, storage keys, config, frontend types)
  - [x] 1.1 Add backend data models
    - Add Pydantic models to `models.py`: `VerificationResult`, `EvidenceCoverage` (`full`/`partial`/`none`), `EvidenceStatus`, `AnswerSpan`, `EvidenceItem`, `Claim`; extend `QueryResponse`/`UnifiedQueryResponse` with `claims: list[Claim]` and `claim_decomposition_failed: bool = False`
    - Make `EvidenceItem` a **discriminated model** keyed on `kind` (`"document"` | `"database"`): document fields optional (`quote`, `source_start`, `source_end`, `document_id`, `document_version`); database fields (`table`, `row_fields`, `sql`, `sql_query_id`, `sql_result_fixture_id`, `row_index`); carry `verification_result`, `coverage: EvidenceCoverage`, and `covered_subclaims: list[int]`; add a per-kind `model_validator` (document → requires quote + offsets + document_id + document_version; database → requires table + row_fields)
    - Add `ClarificationRecord`, `ClarificationPrompt`, `ReasonCode`, `AbstentionResponse`
    - Add `DocumentRecord.owner`, `DocumentVersion`, `IngestionEvent`, `DocumentVersionIndex`, `CorpusPage`
    - Add `ReviewStatus`, `FailureCategory`, `FeedbackReviewRecord`, `FeedbackContext`
    - Add `RelevanceLabels`, `BenchmarkCase`, `DeterministicCheck`, `RetrievalMetrics`, `LLMJudgeScores`, `BenchmarkResult`
    - Add `ReplayRunState`, `ReplayRetrievalParams`, `ReplayRunRequest`, `ReplayRunResult`, `ReplayRun`, `CorpusSnapshot`, `SqlResultFixture` (include `normalized_sql_hash` so fixtures key on `(corpus_snapshot_id, normalized_sql_hash)`, R8.6)
    - Add `AIConfigurationVersion`, `ActivationEvent`, `AIConfigurationIndex`, plus trace additions in `observability_tracing/models.py`
    - Add `Recommendation`, `TraceDiagnosis`, `KnowledgeGapTopic`, `KnowledgeGapMap`
    - _Requirements: 1.1, 1.2, 1.14, 2.2, 3.7, 3.8, 4.11, 5.4, 6.2, 7.7, 8.1, 8.7, 9.1, 10.3, 11.2_

  - [x] 1.2 Add S3 artifact storage key functions
    - Add key-function helpers to `storage.py` for document versions/index/ingestions, clarifications, feedback index, evaluation sets/runs, ai_config versions/index, corpus snapshots + sql fixtures, replays, and knowledge gap map, following the existing key-function convention
    - Wire create-only (`if_none_match`) and ETag-CAS write helpers for immutable and contended keys
    - _Requirements: 5.5, 8.1, 9.7_

  - [x] 1.3 Add configuration settings and thresholds
    - Add answer-path/abstention settings: `route_min_confidence` (per-route min confidence, default `0.5`), `retrieval_score_threshold` (default `0.3`), `clarification_expiry_minutes` (default `30`)
    - Add corpus/listing settings: `corpus_page_size` (default `50`, max `100`), `pagination_signing_key` (HMAC secret for cursor signing)
    - Add evaluation/judge settings: `retrieval_metric_depth_k` (default `10`), `llm_judge_model_id` (default `"gemini-3.1-pro"`), `llm_judge_thinking_budget` (bounded, default `4096`), `llm_judge_read_timeout_s` (default `55`), `llm_judge_per_case_timeout_s` (default `60`), `llm_judge_schedule_interval_hours` (default `24`)
    - Add trace-investigator settings: `trace_investigator_model_id` (default `"gemini-3.1-pro"`), `trace_investigator_thinking_budget` (bounded), `trace_investigator_read_timeout_s`
    - Add knowledge-gap settings: `knowledge_gap_max_topics` (default `25`), `knowledge_gap_min_eligible_outcomes` (default `20`)
    - Add replay settings: `replay_job_timeout_s` (default `300`), `model_pricing` (map of model id → prompt/completion USD per 1K tokens, with entries for `gemini-3.5-flash` and `gemini-3.1-pro`)
    - _Requirements: 2.7, 3.1, 3.6, 4.4, 4.6, 7.6, 7.8, 8.7, 10.3, 11.1, 11.6_

  - [x] 1.4 Add frontend TypeScript types
    - Mirror all new backend response shapes in `api/types.ts`: claim/evidence (discriminated `document`/`database` `EvidenceItem` + `coverage`), answer-span, clarification prompt, abstention response, corpus page, document version/history, feedback context, evaluation results, replay run + result, ai config history, trace diagnosis, knowledge gap map; add `is_operator: boolean` on the `UserPublic` type for operator-only nav gating
    - _Requirements: 1.14, 2.2, 3.8, 4.4, 6.2, 8.7, 9.5, 10.3, 11.2_

  - [x] 1.5 Implement the operator authorization model
    - Add `is_operator: bool` (default `False`) to `UserRecord` and `UserPublic` in `src/rag_system/auth/models.py`, and copy the flag in `UserRecord.to_public()`
    - Add an `operator_emails` allow-list setting to `config.py` (set/list of normalized emails, default empty)
    - Add a `require_operator` FastAPI dependency in the auth layer that reuses the existing bearer/token dependency, resolves operator status as **stored `is_operator` flag OR membership in `operator_emails`**, and rejects a non-operator with `403 operator_required` (unauthenticated callers keep the existing `401`)
    - Note: all operator-only endpoints depend on `require_operator` — corpus admin/restore, feedback inbox + actions, evaluation runs, replay endpoints, corpus-snapshots, ai-config change/history/rollback/approve, trace diagnose, and knowledge-gap generation; `GET /corpus` stays owner-scoped (not operator-gated)
    - _Requirements: 4.2, 4.3_

  - [x]* 1.6 Write tests for the operator model and authorization
    - Cover `to_public()` copying `is_operator`, the resolved-operator rule (stored flag OR allow-list membership), and `require_operator` returning `403 operator_required` for a non-operator while allowing an operator (representative gated endpoint)
    - _Requirements: 4.2, 4.3_

  - [x] 1.7 Extend `QueryTraceRecord` for downstream consumers
    - Additively extend `QueryTraceRecord` in `src/rag_system/models.py` (existing readers unaffected) with `sql: str | None`, `claims: list[Claim]`, `claim_evidence_summary: dict[str, int]`, `ai_configuration_version_id: str | None`, `cost: float | None`, `abstention_reason_code: ReasonCode | None`, and `is_clarification: bool = False` (note `retrieved_hits` and `latency_ms` already exist); this record is the shared backbone read by feedback context, evaluation, replay comparison, trace diagnosis, and knowledge-gap eligibility
    - _Requirements: 6.2, 8.7, 9.1, 10.1, 11.1_

- [x] 2. Implement claim-level evidence mapping backend (R1)
  - [x] 2.1 Implement `classify_evidence_status` pure function
    - Create `claims.py`; implement `classify_evidence_status(...)` deriving exactly one `EvidenceStatus` from a claim's evidence items, their `verification_result`, and the `coverage` signal: `supported` when some item is `entails` AND `coverage == full`; `partially_supported` when `entails` items only cover sub-parts (`coverage == partial`) and no item is `full`; `unsupported` when zero items or all `does_not_entail`; `verification_unavailable` when all `undetermined`
    - _Requirements: 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x]* 2.2 Write property test for evidence-status derivation
    - **Property 3: Evidence status is correctly derived from verification results**
    - **Validates: Requirements 1.4, 1.5, 1.6, 1.7, 1.8**

  - [x] 2.3 Implement `ClaimMapper` decomposition and evidence association
    - In `claims.py`, decompose answer text into `Claim`s with stable `claim_id` derived from `(trace_id, claim_index)` and zero-based `answer_span` (start inclusive, end exclusive); associate 0–100 discriminated `EvidenceItem`s — `document` items carry quote + source offsets + `document_id`/`document_version`; `database` items carry `table`/`row_fields`/`sql`/`sql_query_id`/`sql_result_fixture_id`/`row_index` — enforcing the per-kind validator
    - Claim decomposition is LLM-based via the generation model (`gemini-3.5-flash`) with a structured prompt returning factual statements + spans; on model error/timeout/unparseable output yield an empty claims list (surfaced via the `claim_decomposition_failed` flag in 2.6), never raising
    - Per (claim, evidence) verification is produced by a structured LLM entailment call to the same generation model returning a `VerificationResult` (`entails`/`does_not_entail`/`undetermined`) **and a `coverage` value (`full`/`partial`/`none`)** plus optional `covered_subclaims`; any model error, timeout, or unparseable/low-confidence response for a pair yields `undetermined` with `coverage == none`
    - Detect conflicting evidence during verification: flag a claim when it has ≥1 `EvidenceItem` with `entails` AND ≥1 with `does_not_entail` from different `document_id`s, and surface the flag for the abstention gate (R3.4)
    - Model outputs are stubbed in tests for determinism
    - _Requirements: 1.1, 1.2, 1.3, 3.4_

  - [x]* 2.4 Write property test for claim structure well-formedness
    - **Property 1: Claim structure is well-formed** (offsets satisfy `0 <= start <= end <= len(answer)`, one status per claim, stable ids across re-reads)
    - **Validates: Requirements 1.1, 1.14**

  - [x]* 2.5 Write property test for evidence-item bounds and shape
    - **Property 2: Evidence items are well-formed and bounded** (0–100 items; both `document` and `database` kinds satisfy the per-kind validator; offsets/source ids for documents, table/row_fields for databases; `verification_result` + `coverage` present)
    - **Validates: Requirements 1.2, 1.3**

  - [x] 2.6 Integrate `ClaimMapper` into `generation.py`
    - Call the mapper after prose + citation validation; run the LLM entailment/verification call per (claim, evidence) pair producing `verification_result` + `coverage`; populate `claims`, per-claim `evidence_status` (derived from coverage via `classify_evidence_status`), and the per-claim conflicting-evidence flag; on decomposition failure return empty claims list with `claim_decomposition_failed = true` without raising
    - _Requirements: 1.9, 1.14, 3.4_

  - [x]* 2.7 Write unit test for claim decomposition failure path
    - Assert answer returned with empty claims and `claim_decomposition_failed` flag set
    - _Requirements: 1.9_

  - [x]* 2.8 Write round-trip property test for Claim/EvidenceItem serialization
    - Serialize then deserialize `Claim` (with embedded `EvidenceItem`s of **both** `document` and `database` kinds, plus `coverage`/`covered_subclaims`) preserves all fields
    - _Requirements: 1.14_

- [x] 3. Implement evidence-based abstention backend (R3)
  - [x] 3.1 Implement `evaluate_abstention` pure function
    - Create `abstention.py`; centralize the six triggers with fixed deterministic precedence, returning at most one `AbstentionResponse` with exactly one `reason_code` and a 1–1000 char `missing_information`, and no answer/claims/evidence content
    - `unsupported_claims` uses the concrete materiality rule: every decomposed factual `Claim` is material by default (configurable predicate), so the trigger fires when any claim has `evidence_status == unsupported`
    - `conflicting_evidence` uses the concrete rule from verification: a claim has ≥1 `entails` and ≥1 `does_not_entail` evidence item from different `document_id`s (consuming the flag produced in 2.3/2.6)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [x]* 3.2 Write property test for reason-code selection
    - **Property 9: Abstention selects exactly the correct reason code**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

  - [x]* 3.3 Write property test for abstention response shape
    - **Property 10: Abstention responses carry no answer content and a bounded description**
    - **Validates: Requirements 3.7, 3.8**

- [x] 4. Implement ambiguity clarification backend (R2)
  - [x] 4.1 Add ambiguous classification and clarification issuance
    - Add `ambiguous` (and `scope_ambiguous`) outcome to the classifier in `router.py`; create `clarification.py` with `ClarificationRecord` create-only persistence binding an unguessable `clarification_id` (`secrets.token_urlsafe`) to conversation turn, document scope, original question, and expiry; return a `ClarificationPrompt` instead of routing when ambiguous and not already a reply
    - _Requirements: 2.1, 2.2, 2.9_

  - [x]* 4.2 Write property test for clarification prompt well-formedness
    - **Property 5: Clarification prompts are well-formed and unguessable**
    - **Validates: Requirements 2.2**

  - [x] 4.3 Implement clarification reply processing
    - In `clarification.py`, validate existence/expiry/non-empty reply; re-run the answer path with the combined question scoped to the record's `document_scope` and the ambiguous branch disabled; abstain if still unresolved
    - _Requirements: 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x]* 4.4 Write property test for reply scoping
    - **Property 6: Clarification replies are scoped to their clarification record**
    - **Validates: Requirements 2.4**

  - [x]* 4.5 Write property test for invalid/expired/empty reply rejection
    - **Property 7: Invalid or expired clarification replies are rejected**
    - **Validates: Requirements 2.5, 2.6**

  - [x]* 4.6 Write property test for single-clarification-then-abstention
    - **Property 8: At most one clarification, then abstention**
    - **Validates: Requirements 2.7, 2.8**

  - [x]* 4.7 Write unit test for scope-ambiguity clarification wording
    - Assert clarification asks whether to search selected Documents or the entire Corpus
    - _Requirements: 2.9_

- [x] 5. Wire the unified answer path (R1 + R2 + R3)
  - [x] 5.1 Wire decision points into `api.py` answer endpoints
    - Integrate classify → clarification gate → retrieval gates → generate + claim mapping → abstention gates into `/ask` and `/ask/stream`; register `POST /ask/clarify` with `clarification_invalid_or_expired` and `clarification_reply_required` errors; return answers with claims + evidence, clarification prompts, or abstention payloads
    - `/ask/stream` emits stage-progress events (`classify`/`retrieve`/`generate`/`verify`) for liveness but **holds answer content** — it does not forward generated tokens — until the abstention gates and claim-verification have run, then ends with exactly **one terminal event** carrying one of: the answer with claims/evidence, a `Clarification_Prompt`, or an `Abstention_Response` (no answer content); a post-generation abstention therefore leaks no tokens
    - _Requirements: 1.14, 2.1, 2.3, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x]* 5.2 Write integration test for the answer path
    - Exercise answer-with-claims, clarification, and abstention branches through `api.py` against fakes
    - _Requirements: 1.14, 2.1, 3.1_

- [x] 6. Checkpoint - Answer trustworthiness backend
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement answer trustworthiness frontend (R1, R2, R3)
  - [x] 7.1 Implement `ClaimList` with status indicators
    - Create `components/answer/ClaimList.tsx` rendering each claim with a status indicator distinct per status and not color-only (icon + text label + shape) for all four statuses
    - _Requirements: 1.10, 1.13_

  - [x]* 7.2 Write render test for distinct non-color status indicators
    - **Property 4: Evidence status indicators render distinctly without relying on color**
    - **Validates: Requirements 1.10, 1.13**

  - [x] 7.3 Implement `EvidencePanel` and claim selection
    - Create `components/answer/EvidencePanel.tsx`; selecting a `supported`/`partially_supported` claim displays evidence items (quote/row values, source document, version); evidence fetch/render failure shows an "evidence unavailable" notice while preserving answer and claims
    - _Requirements: 1.11, 1.12_

  - [x]* 7.4 Write tests for claim selection and evidence-unavailable path
    - Cover panel open on selection and the preserved-answer failure notice (MSW-mocked)
    - _Requirements: 1.11, 1.12_

  - [x] 7.5 Implement `ClarificationCard` reply flow
    - Create `components/answer/ClarificationCard.tsx`; detect a clarification payload, render question + reply input, submit to `/ask/clarify`, and surface an inline error on expired/invalid responses
    - _Requirements: 2.3_

  - [x]* 7.6 Write tests for the clarification flow
    - Cover reply submission, expired/invalid `clarification_id` handling, and the post-clarification abstention path
    - _Requirements: 2.3, 2.5, 2.8_

  - [x] 7.7 Implement `AbstentionNotice`
    - Create `components/answer/AbstentionNotice.tsx` displaying `missing_information` and surfacing `reason_code`, never an answer; fall back to a default insufficient-evidence notice when the description is absent
    - _Requirements: 3.9, 3.10_

  - [x]* 7.8 Write tests for abstention display and default fallback
    - Cover reason-code display and the default-notice fallback (MSW-mocked)
    - _Requirements: 3.9, 3.10_

  - [x]* 7.9 Write frontend streaming test for the held-answer contract
    - Assert the stream renders stage-progress events but shows **no answer tokens before the terminal event**, and that a terminal abstention event yields no answer content (only the abstention notice) (MSW-mocked stream)
    - _Requirements: 3.7_

- [x] 8. Implement full corpus inventory (R4)
  - [x] 8.1 Implement corpus listing service
    - Create `corpus.py`: owner-based role scoping (operators see the full backend corpus; a non-operator sees only Documents whose `owner` equals their authenticated identity), applied from the `auth` identity before pagination so the owner filter and cursor window compose consistently
    - Cursor pagination with an opaque base64 token encoding the sort key + last id, signed with HMAC-SHA256 over the payload keyed by `pagination_signing_key`; verify the signature on decode and reject any tampered/truncated/otherwise invalid token with `invalid_cursor` (never trust or silently reset)
    - Page-size clamp to `corpus_page_size` max with null final cursor, sort by `name`/`owner`/`date` + direction, filter by `status`/`owner`/`date`/`active version`, case-insensitive metadata search (1–200 chars), owner included per document; reject `>200`-char search with `search_term_too_long`
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.11, 4.14_

  - [x]* 8.2 Write property test for role-based scoping
    - **Property 11: Corpus listing scoping by role**
    - **Validates: Requirements 4.2, 4.3**

  - [x]* 8.3 Write property test for cursor pagination partitioning
    - **Property 12: Cursor pagination partitions the corpus exactly once**
    - **Validates: Requirements 4.4, 4.5**

  - [x]* 8.4 Write property test for sort/filter/search consistency
    - **Property 13: Sort and filter are consistent across pages**
    - **Validates: Requirements 4.7, 4.8, 4.9, 4.11**

  - [x]* 8.5 Write property test for invalid cursor rejection
    - **Property 14: Invalid cursor is rejected**
    - **Validates: Requirements 4.6**

  - [x] 8.6 Add `GET /corpus` endpoint
    - Register the cursor-paginated endpoint in `api.py` returning `CorpusPage` with sort/filter/search params, owner-scoped to the authenticated identity for non-operators, and structured errors (`invalid_cursor` on HMAC-signature verification failure, `search_term_too_long`)
    - _Requirements: 4.1, 4.2, 4.3, 4.6, 4.14_

  - [x] 8.7 Implement server-paginated `DocumentsPage`
    - Update `pages/DocumentsPage.tsx` to render each returned document independent of browser-local state, with next-cursor navigation, empty-state message, and an error state retaining previously displayed documents
    - _Requirements: 4.1, 4.10, 4.12, 4.13_

  - [x]* 8.8 Write frontend tests for corpus listing
    - Cover next-cursor navigation, empty state, and error-retains-previous (MSW-mocked)
    - _Requirements: 4.10, 4.12, 4.13_

- [x] 9. Implement document version control (R5)
  - [x] 9.1 Formalize versions and ingestion events on ingestion
    - Extend `service.py` ingestion to create a `DocumentVersion` + succeeded `IngestionEvent` and set it active on success; on failure create no version, leave active unchanged, and record a failed `IngestionEvent`; maintain the version index via CAS so at most one active version holds; retain all version source content
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x]* 9.2 Write property test for ingestion outcome records
    - **Property 15: Ingestion outcome determines version and event records**
    - **Validates: Requirements 5.1, 5.2, 5.3**

  - [x]* 9.3 Write property test for version invariants across operations
    - **Property 16: Document version invariants hold across operation sequences**
    - **Validates: Requirements 5.4, 5.5**

  - [x] 9.4 Implement history and restore endpoints
    - Add `GET /documents/{id}/versions` (versions + events newest-first) and `POST /documents/{id}/versions/{version}/restore` (operator-only, depends on `require_operator`) in `service.py` + `api.py`: flip active if vectors exist, re-index from retained source if cleaned up, `version_not_found` (404) with active unchanged for unknown version, retain all prior versions; ensure retrieval uses the active version
    - _Requirements: 5.6, 5.7, 5.8, 5.9, 5.10, 5.11_

  - [x]* 9.5 Write property test for history ordering and active-version retrieval
    - **Property 17: History ordering and retrieval use the active version**
    - **Validates: Requirements 5.6, 5.7**

  - [x]* 9.6 Write property test for restore preserving prior versions
    - **Property 18: Restore preserves prior versions and activates the target**
    - **Validates: Requirements 5.8, 5.10, 5.11**

  - [x]* 9.7 Write unit test for restore requiring re-index from source
    - Cover the cleaned-up-vectors path re-indexing from retained source before activating
    - _Requirements: 5.9_

  - [x]* 9.8 Write round-trip property test for `DocumentVersionIndex`
    - Serialize/deserialize preserves ordered versions and active pointer
    - _Requirements: 5.4_

  - [x] 9.9 Implement `VersionHistory` frontend component
    - Create `components/documents/VersionHistory.tsx` listing versions/events newest-first with a restore action and confirmation
    - _Requirements: 5.7, 5.8_

  - [x]* 9.10 Write frontend tests for version history and restore
    - Cover ordering display and restore confirmation (MSW-mocked)
    - _Requirements: 5.7, 5.8_

- [x] 10. Checkpoint - Corpus and versioning
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement feedback review inbox (R6)
  - [x] 11.1 Implement inbox listing with full context
    - Create `feedback.py`: return cursor-paginated negative-rating (1–2) feedback in reverse-chronological order (empty collection when none), joining each item with the enriched `QueryTraceRecord` (1.7) to read expected answer, confidence, route, `retrieved_hits`, and `sql`, with empty values for absent SQL/comment/expected-answer (and when the joined record is missing/expired); filter by `review_status`
    - `GET /feedback` is operator-only (depends on `require_operator`)
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x]* 11.2 Write property test for negative-rating inbox listing
    - **Property 19: Feedback inbox returns exactly the negative-rating items, paginated and filtered**
    - **Validates: Requirements 6.1, 6.4**

  - [x]* 11.3 Write property test for feedback context completeness
    - **Property 20: Feedback context is complete with empty values for absent fields**
    - **Validates: Requirements 6.2, 6.3**

  - [x] 11.4 Implement classify, promote, and resolve actions
    - Add `POST /feedback/{id}/classify` (validate the six categories, `invalid_failure_category` otherwise; persist category, reviewer, timestamp; set `reviewed`, replacing prior), `POST /feedback/{id}/promote` (create one `BenchmarkCase`; `expected_answer_required` when missing; `already_in_evaluation_set` when duplicate), and `POST /feedback/{id}/resolve` (set `resolved`, keep in inbox) in `feedback.py` + `api.py`; all three actions are operator-only (depend on `require_operator`)
    - _Requirements: 6.5, 6.6, 6.7, 6.8, 6.10, 6.11_

  - [x]* 11.5 Write property test for classification round-trip and transition
    - **Property 21: Classification round-trips and transitions state**
    - **Validates: Requirements 6.5, 6.10**

  - [x]* 11.6 Write property test for guarded idempotent promotion
    - **Property 22: Promotion is guarded and idempotent**
    - **Validates: Requirements 6.6, 6.7, 6.11**

  - [x]* 11.7 Write property test for resolved-item visibility
    - **Property 23: Resolving keeps the item in the inbox**
    - **Validates: Requirements 6.8**

  - [x] 11.8 Implement `FeedbackInboxPage`
    - Create `pages/FeedbackInboxPage.tsx` listing each item with full context, a `review_status` filter, classify/resolve/promote actions, and pagination
    - _Requirements: 6.9_

  - [x]* 11.9 Write frontend tests for the feedback inbox
    - Cover `review_status` filtering, resolved-item visibility, full-context display, and classify/promote actions (MSW-mocked)
    - _Requirements: 6.4, 6.8, 6.9_

- [x] 12. Implement multi-method evaluation system (R7)
  - [x] 12.1 Implement deterministic checks
    - Extend `evaluation.py` so the deterministic method produces per-check `pass`/`fail` for citation presence, each required fact, and evidence-status correctness; a CI run fails iff any deterministic check fails, independent of LLM scores; enforce ≥1 human-reviewed case at set validation
    - Evaluation-run trigger endpoints in `api.py` are operator-only (depend on `require_operator`); evaluation reads the enriched `QueryTraceRecord` (1.7) for per-query context (route, `retrieved_hits`, `claims`/`evidence_status`, `latency_ms`, `cost`)
    - _Requirements: 7.1, 7.4, 7.5_

  - [x]* 12.2 Write property test for deterministic checks and CI status
    - **Property 24: Deterministic checks and CI status**
    - **Validates: Requirements 7.1, 7.5, 7.6**

  - [x] 12.3 Implement retrieval metrics
    - Create `retrieval_metrics.py` computing recall@k, precision@k, and MRR@k at configured depth against `Relevance_Labels` when present; skip entirely when labels absent
    - _Requirements: 7.2, 7.9_

  - [x]* 12.4 Write property test for label-gated retrieval metrics
    - **Property 25: Retrieval metrics are gated on relevance labels and well-formed**
    - **Validates: Requirements 7.2, 7.9**

  - [x] 12.5 Implement LLM judge scoring and scheduling
    - Create `llm_judge.py` producing faithfulness/relevance in `[0.0, 1.0]` when enabled, on the `llm_judge_schedule_interval_hours` schedule, excluded from CI pass/fail; record all results per case
    - The judge uses `llm_judge_model_id` (`gemini-3.1-pro`, a thinking model) via Vertex AI (GCP only, reusing the existing Vertex client/credentials) — NOT `gemini_model_id`; configure a bounded `llm_judge_thinking_budget` and its own `llm_judge_read_timeout_s` (~55s) sized to fit inside the fixed `llm_judge_per_case_timeout_s` (60s) per-case timeout
    - On per-case timeout record an error indication in `LLMJudgeScores.error` while retaining deterministic + retrieval results
    - _Requirements: 7.3, 7.6, 7.7, 7.8_

  - [x]* 12.6 Write property test for bounded scores and result round-trip
    - **Property 26: LLM judge scores are bounded and evaluation results round-trip**
    - **Validates: Requirements 7.3, 7.7**

  - [x]* 12.7 Write unit tests for evaluation edge cases
    - Cover set validation requiring ≥1 human-reviewed case and LLM judge timeout recording an error indication
    - _Requirements: 7.4, 7.8_

  - [x] 12.8 Implement `EvaluationPage`
    - Create `pages/EvaluationPage.tsx` presenting run results by method
    - _Requirements: 7.7_

  - [x]* 12.9 Write frontend tests for the evaluation dashboard
    - Cover rendering deterministic/retrieval/LLM result sections (MSW-mocked)
    - _Requirements: 7.7_

- [x] 13. Checkpoint - Feedback and evaluation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Implement replay and compare lab (R8)
  - [x] 14.1 Implement corpus snapshots and SQL result fixtures
    - In `replay.py`, create immutable `CorpusSnapshot` (manifest of document/version pairs) and `SqlResultFixture` via create-only writes
    - _Requirements: 8.1, 8.6_

  - [x]* 14.2 Write round-trip and immutability property test for snapshots
    - Serialize/deserialize preserves the manifest and a created snapshot cannot be mutated (second create-only write fails)
    - _Requirements: 8.1_

  - [x] 14.3 Implement replay request validation and queued creation
    - In `replay.py` + `api.py`, `POST /replays` (operator-only, depends on `require_operator`) validates the `ai_configuration_version_id` by loading the referenced `AIConfigurationVersion` and checking its `approved` flag is true (and that prompt/model are drawn from that approved version) → else `approved_configuration_required`; validate retrieval params in range (max passages 1–100, min score 0.00–1.00), and existing `corpus_snapshot_id`; missing/out-of-range/unknown → 400 naming the setting; on success create a `queued` run and return its id without blocking
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

  - [x]* 14.4 Write property test for acceptance and queued creation
    - **Property 27: Replay acceptance and queued creation**
    - **Validates: Requirements 8.1, 8.2**

  - [x]* 14.5 Write property test for invalid/unapproved rejection
    - **Property 28: Replay rejects invalid or unapproved configuration**
    - **Validates: Requirements 8.3, 8.4**

  - [x] 14.6 Implement replay worker execution and lifecycle
    - Extend the queue/worker pattern (`worker.py` + `replay.py`) to transition `queued` → `running`, execute the question under the referenced config with **snapshot-scoped retrieval** — retrieve only against the `(document_id, document_version)` pairs in the referenced `CorpusSnapshot.manifest`, not the corpus's current active versions
    - Reproduce SQL-route results from stored fixtures (never live data): compute the `(corpus_snapshot_id, normalized_sql_hash)` key from the query and look up the `SqlResultFixture` by that key; a **missing fixture fails the run** (`failed`, `failure_reason` naming the missing fixture)
    - Success → `completed` recording answer, discriminated `EvidenceItem` evidence, route, retrieval scores (0.00–1.00), latency ms, prompt/completion tokens, and cost computed via the pricing helper (14.14); failure/timeout (`replay_job_timeout_s`) → `failed` with reason and no partial results; on `cancel_requested`, stop at a stage boundary → `cancelled` with no results
    - _Requirements: 8.5, 8.6, 8.7, 8.8, 8.9_

  - [x]* 14.7 Write property test for run lifecycle and result recording
    - **Property 29: Replay run lifecycle records results only on success**
    - **Validates: Requirements 8.5, 8.7, 8.8, 8.9, 8.10**

  - [x]* 14.8 Write property test for SQL-route historical fixtures
    - **Property 30: SQL-route replay uses historical fixtures, not live data** (fixture matched by `(corpus_snapshot_id, normalized_sql_hash)`; missing fixture → `failed` run, never a live query)
    - **Validates: Requirements 8.6**

  - [x] 14.9 Add replay status endpoint and round-trip coverage
    - Add `GET /replays/{id}` (operator-only, depends on `require_operator`) returning current state in `api.py`; add a round-trip test for `ReplayRun`/`ReplayRunResult` serialization
    - _Requirements: 8.10_

  - [x] 14.10 Implement `ReplayLabPage` and `ComparisonView`
    - Create `pages/ReplayLabPage.tsx` to initiate runs, poll state, and a `ComparisonView` showing two completed runs side by side across answer/evidence/route/scores/latency/tokens/cost
    - _Requirements: 8.11_

  - [x]* 14.11 Write frontend tests for replay states and comparison
    - Cover asynchronous run-state transitions and the two-run side-by-side comparison (MSW-mocked)
    - _Requirements: 8.11_

  - [x] 14.12 Add `POST /corpus-snapshots` endpoint
    - Register an operator-only endpoint in `api.py` that captures the current active-version manifest (`list[(document_id, document_version)]`) as an immutable, create-only `CorpusSnapshot` (via the 14.1 snapshot logic), with an optional document-subset scope and optional `SqlResultFixture` capture; return the `corpus_snapshot_id`
    - _Requirements: 8.1, 8.6_

  - [x]* 14.13 Write unit test for the corpus-snapshots endpoint
    - Cover operator-only authorization, optional scope, and immutability (create-only, no overwrite)
    - _Requirements: 8.1_

  - [x] 14.14 Implement replay cost computation from the pricing map
    - In `replay.py`, implement a pure cost helper using `model_pricing`: `cost = prompt_tokens / 1000 * price_in + completion_tokens / 1000 * price_out` for the model recorded on the run's `AI_Configuration_Version`; a model absent from the map contributes `0.0` (and is logged) so an unpriced model never fails the run
    - _Requirements: 8.7_

  - [x]* 14.15 Write unit test for replay cost computation
    - Cover a priced model (correct prompt/completion arithmetic) and an unpriced model resolving to `0.0`
    - _Requirements: 8.7_

  - [x] 14.16 Add `POST /replays/{id}/cancel` endpoint
    - In `replay.py` + `api.py`, register an operator-only (`require_operator`) cancel endpoint that sets `cancel_requested = true` and transitions a `queued`/`running` run to `cancelled` with no results (the worker checks the flag at stage boundaries); cancelling a run already in a terminal state (`completed`/`failed`/`cancelled`) is a no-op
    - _Requirements: 8.9_

  - [x]* 14.17 Write unit test for replay cancellation
    - Cover operator-only authorization, `queued`/`running` → `cancelled` with no results, and the terminal-state no-op
    - _Requirements: 8.9_

  - [x] 14.18 Add `GET /corpus-snapshots` listing endpoint
    - Register an operator-only (`require_operator`) endpoint in `api.py` listing existing `CorpusSnapshot`s (id + `created_at` + manifest size) so an operator can pick one when initiating a replay
    - _Requirements: 8.1_

  - [x]* 14.19 Write unit test for the corpus-snapshots listing endpoint
    - Cover operator-only authorization and the returned id/`created_at`/manifest-size shape
    - _Requirements: 8.1_

- [x] 15. Implement versioned AI configuration (R9)
  - [x] 15.1 Implement AI configuration versioning service
    - Create `ai_config.py` (all endpoints operator-only, depend on `require_operator`): `PUT /ai-config/{id}` creates an immutable version for a 1–500 char description (`change_description_required` otherwise, no new version, active unchanged); `GET /ai-config/{id}/history` returns versions + descriptions reverse-chronologically (empty when none); `POST /ai-config/{id}/rollback` sets an existing version active and records an `ActivationEvent` (operator, previous, selected, timestamp, reason), `configuration_version_not_found` (404) with active unchanged for unknown, retaining all prior versions
    - _Requirements: 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10_

  - [x]* 15.2 Write property test for change-description validation
    - **Property 32: AI configuration change validation**
    - **Validates: Requirements 9.3, 9.4**

  - [x]* 15.3 Write property test for immutability and ordered history
    - **Property 33: AI configuration versions are immutable and history is ordered**
    - **Validates: Requirements 9.5, 9.7**

  - [x]* 15.4 Write property test for rollback activation and retention
    - **Property 34: Rollback activates the target, audits it, and retains all versions**
    - **Validates: Requirements 9.8, 9.9, 9.10**

  - [x] 15.5 Register AI config endpoints
    - Wire the operator-only `ai-config` routes (`require_operator`) in `api.py` and add a unit test for the empty-history case
    - _Requirements: 9.5, 9.6_

  - [x] 15.6 Record producing configuration version on traces with redaction
    - Extend `observability_tracing` so each recorded trace carries the producing `ai_configuration_version_id` **resolved by `AIConfigResolver` (15.12)** and its resolved settings, redacting sensitive values on a copy of the settings (never mutating the source) using the defined sensitive-key patterns (`api_key`/`secret`/`token`/`credential`/`password`), including nested `retrieval_settings` and `reranker_config`; record an `unresolved` indicator retaining other trace data when the version cannot be resolved
    - _Requirements: 9.1, 9.2, 9.11_

  - [x]* 15.7 Write property test for trace config recording with redaction
    - **Property 31: Traces record the producing configuration version with secrets redacted**
    - **Validates: Requirements 9.1, 9.11**

  - [x] 15.8 Implement `AIConfigHistory` frontend component
    - Create `components/observability/AIConfigHistory.tsx` with a history view plus rollback and reason capture
    - _Requirements: 9.5, 9.8_

  - [x]* 15.9 Write frontend tests for AI config history and rollback
    - Cover reverse-chronological history rendering and rollback reason capture (MSW-mocked)
    - _Requirements: 9.5, 9.8_

  - [x] 15.10 Implement AI configuration version approval endpoint
    - Add `POST /ai-config/{id}/versions/{version_id}/approve` (operator-only) in `ai_config.py` + `api.py`: set the target version's `approved = true` and record `approver` + `approved_at`; unknown version → `configuration_version_not_found`; non-operator → `operator_required`; approval does not mutate the version's governed settings
    - _Requirements: 8.3, 9.7_

  - [x]* 15.11 Write unit test for the approval endpoint
    - Cover successful approval (sets `approved`/`approver`/`approved_at`), `configuration_version_not_found` for an unknown version, and `operator_required` for a non-operator caller
    - _Requirements: 8.3, 9.7_

  - [x] 15.12 Implement `AIConfigResolver`
    - In `ai_config.py`, implement `AIConfigResolver.resolve(config_id)` that loads the active `AIConfigurationVersion` (via `AIConfigurationIndex.active_version_id`) and applies its settings bundle across the pipeline — router threshold, retrieval settings, reranker config, SQL/generation prompt, model, and output schema — so a single active version governs the whole answer; the resolved `version_id` is what the `Tracing_Service` stamps on the trace (R9.1, consumed by 15.6); bootstrap and persist a seeded default version from `config.py` defaults when no active version exists
    - _Requirements: 9.1, 9.2_

  - [x]* 15.13 Write unit test for `AIConfigResolver`
    - Cover resolving the active version's settings bundle, the resolved `version_id` used for trace stamping, and the default-bootstrap path when no active version exists
    - _Requirements: 9.1, 9.2_

- [x] 16. Checkpoint - Replay and configuration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 17. Implement AI trace investigator (R10)
  - [x] 17.1 Implement trace diagnosis service
    - Create `trace_investigator.py`: load the recorded, enriched `QueryTraceRecord` (1.7; `trace_not_found` 404 with no diagnosis when absent); analyze `route`, retrieval scores (from `retrieved_hits`), rerank order, and the generation outcome (`claims`/`evidence_status`/`abstention_reason_code`); return a cause description referencing ≥1 analyzed element with 1–10 recommendations each referencing AI configuration or corpus; when no cause, return a "no cause determined" description with zero recommendations; apply no mutations (read-only)
    - Diagnosis is LLM-based using `trace_investigator_model_id` (`gemini-3.1-pro`, a thinking model) with a bounded `trace_investigator_thinking_budget` and its own `trace_investigator_read_timeout_s`; model outputs are stubbed in tests
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [x]* 17.2 Write property test for diagnosis-output consistency
    - **Property 35: Diagnosis output is consistent with cause determination**
    - **Validates: Requirements 10.1, 10.3, 10.4, 10.5**

  - [x]* 17.3 Write property test for unrecorded-trace error and no mutation
    - **Property 36: Diagnosis of an unrecorded trace errors and never mutates**
    - **Validates: Requirements 10.2, 10.7**

  - [x] 17.4 Add `POST /traces/{id}/diagnose` endpoint
    - Register the operator-only diagnose route (`require_operator`) in `api.py` returning `TraceDiagnosis` (read-only recommendations)
    - _Requirements: 10.1, 10.6_

  - [x] 17.5 Implement `TraceInvestigator` frontend component
    - Create `components/observability/TraceInvestigator.tsx` with a "Diagnose" action showing cause + recommendations as read-only suggestions
    - _Requirements: 10.6_

  - [x]* 17.6 Write frontend tests for the trace investigator
    - Cover cause + recommendation rendering and the read-only (no-mutation) presentation (MSW-mocked)
    - _Requirements: 10.6_

- [x] 18. Implement knowledge gap map (R11)
  - [x] 18.1 Implement knowledge gap clustering and recommendations
    - Create `knowledge_gap.py`: scan stored enriched `QueryTraceRecord`s (1.7) + feedback for eligible outcomes — low-confidence (`confidence_score` below threshold), abstained (`abstention_reason_code` set), negatively rated (joined feedback); gate generation on `knowledge_gap_min_eligible_outcomes` (insufficient → surface the minimum rather than generating)
    - Cluster eligible outcomes using embedding-based clustering (Titan embeddings) bounded by `knowledge_gap_max_topics`; derive each topic's `coverage_quality` (`poor`/`fair`/`good`) from average confidence + negative-feedback-ratio thresholds and a non-negative contributing-question count; label topics via `gemini-3.5-flash` summarization
    - Produce recommended missing topics/source types, documents needing re-ingestion, suggested benchmark cases, and frequently requested topics; on failure return `knowledge_gap_generation_failed`
    - _Requirements: 11.1, 11.2, 11.4, 11.5, 11.6_

  - [x]* 18.2 Write property test for bounded clustering and topic shape
    - **Property 37: Knowledge gap clustering is bounded and topics are well-formed**
    - **Validates: Requirements 11.1, 11.2**

  - [x]* 18.3 Write property test for recommendation categories
    - **Property 38: Knowledge gap map includes all recommendation categories**
    - **Validates: Requirements 11.3, 11.4**

  - [x] 18.4 Add knowledge gap map endpoint
    - Register the operator-only generation route (`require_operator`) in `api.py` and add a unit test for the generation-failure error
    - _Requirements: 11.5_

  - [x] 18.5 Implement `KnowledgeGapMapPage`
    - Create `pages/KnowledgeGapMapPage.tsx` rendering topics with coverage-quality + counts and an insufficient-outcomes notice stating the configured minimum when eligible outcomes are below it
    - _Requirements: 11.3, 11.6_

  - [x]* 18.6 Write frontend tests for the knowledge gap map
    - Cover topic rendering with coverage-quality/counts and the insufficient-outcomes notice (MSW-mocked)
    - _Requirements: 11.3, 11.6_

- [x] 19. Frontend integration - routing, navigation, and API clients
  - [x] 19.1 Wire new pages into routes and navigation
    - Register `FeedbackInboxPage`, `EvaluationPage`, `ReplayLabPage`, and `KnowledgeGapMapPage` (plus the answer/version/observability components) in `App.tsx` routes and add their entries to the `AppShell` nav; gate the operator-only nav entries on the `is_operator` flag from `UserPublic`
    - _Requirements: 4.1, 6.9, 7.7, 8.11, 9.5, 10.6, 11.3_

  - [x] 19.2 Add API client methods and types
    - Add the corresponding methods to `api/client.ts` (corpus, versions/restore, feedback inbox + actions, evaluation runs, replays + cancel + corpus-snapshots, ai-config history/rollback/approve, trace diagnose, knowledge-gap) and fill any missing shapes in `api/types.ts`
    - _Requirements: 4.1, 6.9, 7.7, 8.11, 9.5, 10.6, 11.3_

  - [x]* 19.3 Write route and navigation tests
    - Cover that each new page renders at its route and that operator-only nav entries are shown for an operator and hidden for a non-operator (`is_operator` gating) (MSW-mocked)
    - _Requirements: 4.1, 6.9, 8.11_

- [x] 20. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are test sub-tasks. Per the project testing preference, tests are always required — the `*` marker only distinguishes test sub-tasks from implementation sub-tasks; it does **not** mean they are optional or skippable.
- Each task references specific granular requirements for traceability.
- Each property-based test task references its design property number and the requirements it validates; every task is placed close to the implementation it exercises to catch errors early.
- Round-trip properties are included for all new serialization: Claim/EvidenceItem (2.8), DocumentVersionIndex (9.8), BenchmarkResult (Property 26 / 12.6), CorpusSnapshot (14.2), ReplayRun/ReplayRunResult (14.9), and AIConfigurationVersion (Property 33 / 15.3).
- Backend property/unit tests use pytest/Hypothesis under `tests/` with the existing test doubles for LLM/Pinecone/Postgres/S3; frontend tests use Vitest + Testing Library + MSW colocated `*.test.ts(x)`.
- Checkpoints ensure incremental validation at reasonable breaks.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "1.7"] },
    { "id": 1, "tasks": ["1.6", "2.1", "3.1", "4.1", "8.1", "9.1", "11.1", "12.1", "12.3", "14.1", "15.1", "17.1", "18.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "3.3", "4.2", "4.3", "8.2", "8.3", "8.4", "8.5", "9.2", "9.3", "11.2", "11.3", "12.2", "12.4", "12.5", "14.2", "14.3", "14.14", "15.2", "15.3", "15.12", "17.2", "17.3", "18.2", "18.3"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.6", "4.4", "4.5", "4.6", "4.7", "8.6", "9.4", "11.4", "12.6", "14.4", "14.5", "14.6", "14.12", "14.15", "15.4", "15.5", "15.6", "15.10", "15.13", "17.4", "18.4"] },
    { "id": 4, "tasks": ["2.7", "2.8", "5.1", "8.7", "9.5", "9.6", "9.7", "9.8", "11.5", "11.6", "11.7", "12.7", "14.7", "14.8", "14.9", "14.13", "14.18", "15.7", "15.8", "15.11", "17.5", "18.5"] },
    { "id": 5, "tasks": ["5.2", "7.1", "7.3", "7.5", "7.7", "8.8", "9.9", "11.8", "12.8", "14.10", "14.16", "14.19", "15.9", "17.6", "18.6"] },
    { "id": 6, "tasks": ["7.2", "7.4", "7.6", "7.8", "7.9", "9.10", "11.9", "12.9", "14.11", "14.17", "19.1", "19.2"] },
    { "id": 7, "tasks": ["19.3"] }
  ]
}
```
