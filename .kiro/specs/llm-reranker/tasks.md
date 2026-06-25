# Implementation Plan: LLM Reranker

## Overview

This plan implements an LLM-based reranking stage in the RAG query pipeline. The reranker scores retrieved chunks for relevance using Gemini 3.5 Flash on GCP Vertex AI, reorders them by score, and filters by threshold/top-K before passing to the answer generator. Implementation follows incremental steps: configuration → models → provider → reranker logic → pipeline integration → observability → tests.

## Tasks

- [x] 1. Extend Settings and add RankedHit model
  - [x] 1.1 Add reranker configuration fields to Settings
    - Add `reranker_enabled`, `reranker_model_id`, `reranker_top_k`, `reranker_score_threshold`, `reranker_max_concurrent`, `reranker_timeout_s` fields to `src/rag_system/config.py`
    - Use Pydantic `Field` with `alias`, `default`, `ge`, `le` constraints as specified in the design
    - Validate: `reranker_top_k` 1–100, `reranker_score_threshold` 0.0–1.0 when set, `reranker_max_concurrent` 1–50, `reranker_timeout_s` 1–300, `reranker_model_id` max_length=128
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x] 1.2 Add RankedHit model to models.py
    - Create `RankedHit` Pydantic model in `src/rag_system/models.py` with fields: `chunk: Chunk`, `score: float`, `source: str`, `rerank_score: float`
    - `rerank_score` constrained to [0.0, 1.0] via `Field(ge=0.0, le=1.0)`
    - _Requirements: 1.2, 1.4_

  - [x]* 1.3 Write unit tests for Settings reranker validation
    - Test default values, valid boundary values, and invalid out-of-range values for each reranker field
    - Test that invalid config raises `ValidationError` with field name and constraint info
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

- [x] 2. Implement RerankerProvider
  - [x] 2.1 Create RerankerProvider class in reranker.py
    - Create `src/rag_system/reranker.py` with `RerankerProvider` class
    - Implement lazy Vertex AI SDK initialization (same pattern as existing `GeminiProvider._ensure_initialized`)
    - Implement `score_chunk(query: str, chunk_text: str) -> float` method
    - Build scoring prompt from template (query + chunk_text)
    - Call Gemini 3.5 Flash with temperature=0.0, max_output_tokens=16
    - Parse response to extract float in [0.0, 1.0]; return 0.0 and log warning for non-parseable responses
    - Integrate circuit breaker via `get_circuit_breaker("reranker", failure_threshold=settings.circuit_failure_threshold, recovery_timeout_s=settings.circuit_recovery_timeout_s)`
    - Apply `@retry_on_transient(retryable_exceptions=(ConnectionError, TimeoutError, ...), max_retries=3)` for transient errors
    - _Requirements: 1.1, 5.1, 5.2, 5.3, 5.5_

  - [x]* 2.2 Write property test for score bound invariant
    - **Property 1: Score Bound Invariant**
    - Generate random queries and chunk texts, mock Gemini to return random valid/invalid floats
    - Assert every `rerank_score` in output is in [0.0, 1.0] inclusive
    - **Validates: Requirements 1.2**

  - [x]* 2.3 Write property test for failure yields zero score
    - **Property 5: Failure Yields Zero Score**
    - Generate random non-parseable strings (empty, text, out-of-range floats), mock LLM to return them
    - Assert `rerank_score` is 0.0 for every non-parseable response
    - **Validates: Requirements 5.2, 5.3**

- [x] 3. Implement LLMReranker orchestration
  - [x] 3.1 Create LLMReranker class in reranker.py
    - Implement `LLMReranker.__init__(self, settings: Settings)` — creates `RerankerProvider` and `ThreadPoolExecutor(max_workers=settings.reranker_max_concurrent)`
    - Implement `rerank(self, query: str, hits: list[RetrievalHit]) -> list[RankedHit]`
    - Handle empty input: return empty list without calling LLM
    - Score chunks concurrently using `ThreadPoolExecutor`, processing in sequential batches of `reranker_max_concurrent`
    - Handle per-chunk failures: assign 0.0 score, log warning, continue processing remaining chunks
    - Apply score threshold filter (when `reranker_score_threshold` is set)
    - Apply top-K limit (`reranker_top_k`)
    - Sort output by descending `rerank_score`
    - Enforce overall timeout via `concurrent.futures.wait(timeout=reranker_timeout_s)`
    - _Requirements: 1.1, 1.3, 1.4, 2.1, 2.2, 2.3, 2.5, 7.1, 7.2, 7.3, 7.4_

  - [x]* 3.2 Write property test for data preservation
    - **Property 2: Data Preservation**
    - Generate random `RetrievalHit` lists, mock LLM with valid scores
    - Assert every output `RankedHit` preserves `chunk`, `score`, and `source` from corresponding input
    - **Validates: Requirements 1.4**

  - [x]* 3.3 Write property test for output size, ordering, and threshold filtering
    - **Property 3: Output Size, Ordering, and Threshold Filtering**
    - Generate random hit lists, `reranker_top_k` (1–100), and `reranker_score_threshold` (0.0–1.0 or None)
    - Assert: no output chunk below threshold, output length equals min(top_k, passing_chunks), sorted descending by rerank_score
    - **Validates: Requirements 1.1, 2.1, 2.2, 2.3**

  - [x]* 3.4 Write property test for concurrency bound
    - **Property 6: Concurrency Bound**
    - Generate lists larger than max_concurrent, track peak in-flight calls via threading counter
    - Assert peak concurrency never exceeds `reranker_max_concurrent`
    - **Validates: Requirements 7.1**

  - [x]* 3.5 Write property test for pointwise scoring (one call per chunk)
    - **Property 7: Pointwise Scoring — One Call Per Chunk**
    - Generate lists of varying sizes (1–50), count mock LLM invocations
    - Assert exactly N calls for N input chunks
    - **Validates: Requirements 7.2**

  - [x]* 3.6 Write property test for fault isolation within batch
    - **Property 8: Fault Isolation Within Batch**
    - Generate batches with random failure patterns (some chunks raise exceptions)
    - Assert all non-failed calls complete successfully with valid `rerank_score` values
    - **Validates: Requirements 7.4**

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Integrate reranker into RagService query pipeline
  - [x] 5.1 Add reranker to RagService
    - Add lazy-init `_reranker` property to `RagService` in `src/rag_system/service.py` (double-check locking pattern matching existing properties)
    - In `query()`, after retrieval hits and before answer generation: check `reranker_enabled`, check circuit breaker state, invoke `self.reranker.rerank()` wrapped in try/except
    - On reranker success: convert `list[RankedHit]` back to `list[RetrievalHit]` for `AnswerGenerator`
    - On reranker failure/timeout/circuit open: log warning, fall back to original unreranked hits
    - Default `reranker_enabled` to `False` so existing pipelines are unaffected
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 5.4, 5.6_

  - [x]* 5.2 Write property test for graceful fallback
    - **Property 4: Graceful Fallback Preserves Original Hits**
    - Generate random hit lists, force reranker to raise various exceptions
    - Assert `RagService` passes original unreranked hits to generator unchanged
    - **Validates: Requirements 3.4**

  - [x]* 5.3 Write unit tests for pipeline integration
    - Test reranker is invoked when enabled and results are passed to generator
    - Test reranker is skipped when disabled (no reranker calls, hits pass through)
    - Test fallback on reranker error, timeout, and circuit breaker open
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 5.4, 5.6_

- [x] 6. Add observability instrumentation
  - [x] 6.1 Add metrics and structured logging to LLMReranker
    - Wrap rerank operation with `timed` context manager for duration logging
    - Emit `rag_reranker_duration_ms` metric observation
    - Emit `rag_reranker_input_count` with number of chunks received
    - Emit `rag_reranker_output_count` with number of chunks returned after filtering
    - Increment `rag_reranker_fallback_total` counter with `reason` label (`timeout`, `error`, `circuit_open`) on fallback
    - Log top/min/avg `rerank_score` as structured log fields on success
    - Emit `rag_reranker_top_score` metric observation on success
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x]* 6.2 Write unit tests for observability
    - Verify metrics are emitted with correct names and values
    - Verify fallback counter incremented with correct reason labels
    - Verify structured log fields present on successful rerank
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The reranker module (`src/rag_system/reranker.py`) contains both `RerankerProvider` and `LLMReranker` classes
- Tests should be placed in `tests/test_reranker.py` following existing naming conventions
- All property-based tests use the Hypothesis library with minimum 100 iterations

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.6"] },
    { "id": 4, "tasks": ["5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "6.1"] },
    { "id": 6, "tasks": ["6.2"] }
  ]
}
```
