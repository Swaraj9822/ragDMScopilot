# Requirements Document

## Introduction

This feature adds an LLM-based reranking stage to the RAG query pipeline. After Pinecone hybrid search retrieves candidate chunks, a Gemini 3.5 Flash model on GCP scores each chunk for relevance to the user query. The reranker reorders and filters the retrieved chunks so only the most relevant ones are passed to the answer generator (BedrockNemotronGenerator). This improves answer quality by reducing noise from weakly-relevant retrieval results.

## Glossary

- **Reranker**: A component that accepts a query and a list of retrieved chunks, scores each chunk for relevance using an LLM, and returns the chunks reordered by relevance score.
- **Reranker_Score**: A floating-point relevance score (0.0–1.0) assigned by the LLM to each chunk indicating how relevant the chunk is to the query.
- **Reranker_Prompt**: The prompt template sent to Gemini 3.5 Flash that instructs the model to score a chunk's relevance to a query.
- **RagService**: The orchestration layer that coordinates the query pipeline (embed → retrieve → rerank → generate).
- **PineconeHybridIndex**: The retrieval component that performs dense + BM25 sparse hybrid search against the Pinecone vector index.
- **AnswerGenerator**: The component that produces a grounded answer from a question and a list of ranked retrieval hits.
- **GCP_Vertex_AI**: Google Cloud Platform's Vertex AI service used to invoke Gemini models.
- **Settings**: The pydantic-settings configuration class that provides all environment-driven settings.

## Requirements

### Requirement 1: Reranker Component

**User Story:** As a RAG system operator, I want retrieved chunks to be scored for relevance by an LLM before answer generation, so that the generator receives higher-quality context and produces more accurate answers.

#### Acceptance Criteria

1. WHEN the Reranker receives a query and a list of RetrievalHit objects, THE Reranker SHALL send a scoring prompt to Gemini 3.5 Flash via GCP Vertex AI for each chunk and return the chunks reordered by descending Reranker_Score.
2. THE Reranker SHALL assign each chunk a Reranker_Score between 0.0 and 1.0 inclusive.
3. WHEN the Reranker receives an empty list of RetrievalHit objects, THE Reranker SHALL return an empty list without calling the LLM.
4. THE Reranker SHALL preserve all original RetrievalHit data (chunk content, metadata, original retrieval score) while adding the Reranker_Score.

### Requirement 2: Top-K Filtering After Reranking

**User Story:** As a RAG system operator, I want to control how many chunks survive reranking, so that I can tune the trade-off between context richness and generation cost.

#### Acceptance Criteria

1. THE Reranker SHALL return at most `reranker_top_k` chunks after reranking, sorted in descending order by Reranker_Score, where `reranker_top_k` is a configurable integer setting with a minimum value of 1 and a maximum value of 200.
2. WHEN `reranker_top_k` is greater than or equal to the number of input chunks, THE Reranker SHALL return all input chunks sorted in descending order by Reranker_Score.
3. WHERE a minimum score threshold is configured, THE Reranker SHALL first exclude chunks with a Reranker_Score below the threshold, then apply the `reranker_top_k` limit to the remaining chunks.
4. IF `reranker_top_k` is set to a value less than 1 or greater than 200, THEN THE Reranker SHALL reject the configuration with an error message indicating the value is outside the allowed range.
5. WHEN the input chunk list is empty, THE Reranker SHALL return an empty list without error.

### Requirement 3: Pipeline Integration

**User Story:** As a RAG system operator, I want the reranker to be an optional stage in the query pipeline that can be enabled or disabled via configuration, so that I can compare retrieval quality with and without reranking.

#### Acceptance Criteria

1. WHILE the reranker is enabled via configuration, WHEN the RagService processes a query, THE RagService SHALL invoke the Reranker with the retrieval hits and the original query text, then pass the reranked hits to the AnswerGenerator in the reranker-determined order.
2. WHILE the reranker is disabled via configuration, WHEN the RagService processes a query, THE RagService SHALL pass retrieval hits directly to the AnswerGenerator without invoking the Reranker and without modifying hit order or scores.
3. THE RagService SHALL default the reranker configuration to disabled so that existing query pipelines remain unaffected until an operator explicitly enables reranking.
4. IF the Reranker is enabled but raises an error during query processing, THEN THE RagService SHALL fall back to passing the original retrieval hits to the AnswerGenerator unchanged and SHALL log the reranker failure.

### Requirement 4: Configuration

**User Story:** As a RAG system operator, I want all reranker parameters to be configurable via environment variables, so that I can tune reranking behavior without code changes.

#### Acceptance Criteria

1. THE Settings SHALL expose a `reranker_enabled` boolean field (env var `RAG_RERANKER_ENABLED`, default `false`).
2. THE Settings SHALL expose a `reranker_model_id` string field (env var `RAG_RERANKER_MODEL_ID`, default `gemini-3.5-flash`) with a maximum length of 128 characters.
3. THE Settings SHALL expose a `reranker_top_k` integer field (env var `RAG_RERANKER_TOP_K`, default `10`) accepting values from 1 to 100 inclusive.
4. THE Settings SHALL expose a `reranker_score_threshold` optional float field (env var `RAG_RERANKER_SCORE_THRESHOLD`, default `None`) accepting values from 0.0 to 1.0 inclusive when set.
5. THE Settings SHALL expose a `reranker_max_concurrent` integer field (env var `RAG_RERANKER_MAX_CONCURRENT`, default `5`) accepting values from 1 to 50 inclusive.
6. THE Settings SHALL expose a `reranker_timeout_s` integer field (env var `RAG_RERANKER_TIMEOUT_S`, default `30`) accepting values from 1 to 300 inclusive.
7. IF any reranker configuration field receives a value outside its accepted range or of an incompatible type, THEN THE Settings SHALL reject the configuration at startup with a validation error indicating the field name and the accepted constraint.

### Requirement 5: Resilience and Error Handling

**User Story:** As a RAG system operator, I want the reranker to handle failures gracefully, so that a reranker outage does not prevent queries from being answered.

#### Acceptance Criteria

1. IF the Gemini API returns a transient error (ConnectionError, TimeoutError, or HTTP 429/5xx) during a chunk scoring request, THEN THE Reranker SHALL retry the failed request with exponential backoff starting at 1 second, up to a maximum of 3 attempts, consistent with the existing `retry_on_transient` pattern.
2. IF all retry attempts for a chunk scoring request are exhausted without success, THEN THE Reranker SHALL assign that chunk a Reranker_Score of 0.0 and log a warning.
3. IF the Gemini API returns a non-parseable response (response text that cannot be converted to a float between 0.0 and 1.0) for a chunk scoring request, THEN THE Reranker SHALL assign that chunk a Reranker_Score of 0.0 and log a warning including the raw response content.
4. IF the total reranking operation exceeds the configured `reranker_timeout_s` (default 30 seconds), THEN THE RagService SHALL cancel remaining scoring requests, fall back to using the original unreranked retrieval hits for answer generation, and log a warning indicating the timeout.
5. THE Reranker SHALL use a circuit breaker (consistent with the existing `CircuitBreaker` pattern, default failure threshold of 5 consecutive failures and recovery timeout of 30 seconds) to fail fast when repeated Gemini scoring failures are detected.
6. WHILE the reranker circuit breaker is in the open state, THE RagService SHALL skip the reranking stage entirely and pass the original unreranked retrieval hits directly to the AnswerGenerator without calling the Gemini API.

### Requirement 6: Observability

**User Story:** As a RAG system operator, I want visibility into reranker performance and behavior, so that I can monitor latency, detect degradation, and understand ranking quality changes.

#### Acceptance Criteria

1. THE Reranker SHALL log the reranking operation start and completion with duration using the `timed` context manager pattern.
2. THE Reranker SHALL emit a `rag_reranker_duration_ms` metric observation for each reranking operation.
3. THE Reranker SHALL emit a `rag_reranker_input_count` metric observation recording the number of chunks received for scoring.
4. THE Reranker SHALL emit a `rag_reranker_output_count` metric observation recording the number of chunks returned after filtering.
5. WHEN the Reranker falls back to unreranked results, THE Reranker SHALL increment a `rag_reranker_fallback_total` counter metric with a `reason` label indicating the cause (one of: `timeout`, `error`, `circuit_open`).
6. WHEN reranking completes successfully, THE Reranker SHALL log the top Reranker_Score, minimum Reranker_Score, and average Reranker_Score as structured log fields.
7. WHEN reranking completes successfully, THE Reranker SHALL emit a `rag_reranker_top_score` metric observation recording the highest Reranker_Score from the scored chunks.

### Requirement 7: Concurrency and Batching

**User Story:** As a RAG system operator, I want chunk scoring to be parallelized, so that reranking latency remains acceptable even when scoring many chunks.

#### Acceptance Criteria

1. THE Reranker SHALL score chunks concurrently up to the configured `reranker_max_concurrent` limit, ensuring no more than `reranker_max_concurrent` LLM scoring calls are in-flight simultaneously.
2. THE Reranker SHALL send one LLM call per chunk (pointwise scoring) rather than a single call with all chunks concatenated.
3. WHEN the number of input chunks exceeds `reranker_max_concurrent`, THE Reranker SHALL process chunks in sequential batches of size `reranker_max_concurrent`, where each batch completes before the next batch begins, until all chunks are scored.
4. IF an individual chunk scoring call fails within a concurrent batch, THEN THE Reranker SHALL continue processing the remaining calls in that batch without cancellation.
