# Implementation Plan: AI Observability Platform

## Overview

This plan implements an end-to-end request-tracing and log-persistence platform additively on top of
the existing RAG system in `src/rag_system/`. Work proceeds bottom-up: configuration and domain
models first, then the in-process capture layer (context propagation, sampling, span recording,
buffers), then serialization and PostgreSQL stores, then background flush workers and the log-capture
handler, then the HTTP query APIs, and finally integration into the live request, hybrid, and
ingestion paths plus retention scheduling and app wiring. Each step builds on prior steps and ends by
wiring new components into the running application. Property-based tests (Hypothesis) follow each
implementation step and validate the 33 correctness properties from the design; unit and integration
tests cover edge cases and end-to-end behaviour.

New code lives in a `src/rag_system/observability_tracing/` package; existing modules
(`config.py`, `api.py`, `service.py`, `router.py`, `queue.py`, `worker`) are extended without
changing their observable behaviour.

## Tasks

- [x] 1. Configuration and domain model foundation
  - [x] 1.1 Add tracing settings and sample-rate validator to `config.py`
    - Add `tracing_enabled`, `trace_sample_rate`, `trace_retention_hours`, `log_retention_hours`,
      `retention_interval_hours`, `trace_buffer_capacity`, `log_buffer_capacity` fields using the
      existing pydantic-settings `alias` mechanism
    - Add a `field_validator` rejecting non-numeric / out-of-range `trace_sample_rate` and out-of-bounds
      retention periods (1 hour – 3650 days) at startup
    - _Requirements: 10.6, 10.9_

  - [x] 1.2 Write property test for invalid sample-rate configuration
    - **Property 22: Invalid sample-rate configuration is rejected at startup**
    - **Validates: Requirements 10.6**

  - [x] 1.3 Create `observability_tracing` package and domain models
    - Create the package and define `Span`, `Trace`, `LogRecordModel` dataclasses, `SpanStatus`,
      `AttributeValue`, and the `StoredTrace`/`StoredSpan` typed shapes
    - _Requirements: 1.3, 1.4, 1.7, 5.2, 5.3, 14.2_

- [x] 2. Trace context propagation
  - [x] 2.1 Implement `TraceContextPropagator`
    - Add the `_ACTIVE_SPAN_ID` ContextVar alongside the existing `_TRACE_ID`; implement
      `get_active_trace_id`, `get_active_span_id`, `bind_span`, `restore_span`, and
      `propagate_into_thread` using `contextvars.copy_context()`
    - On propagation failure, run with null context, increment
      `rag_trace_context_propagation_failures_total`, and proceed
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 2.2 Write property test for thread context isolation
    - **Property 31: Thread context does not leak across pooled work**
    - **Validates: Requirements 2.4**

  - [x] 2.3 Write unit tests for context propagation edge cases
    - Test null-context default when no trace active and the propagation-failure metric path
    - _Requirements: 2.2, 2.5_

- [x] 3. Trace sampler
  - [x] 3.1 Implement `TraceSampler`
    - `should_record` returns False when disabled (ignoring `X-Trace-Id`), True when enabled with a
      trace header, otherwise records with probability `sample_rate`
    - _Requirements: 10.1, 10.4, 10.5, 10.7, 10.8_

  - [x] 3.2 Write property test for sampling decision
    - **Property 21: Sampling decision honours enablement, header override, and rate**
    - **Validates: Requirements 10.1, 10.4, 10.7**

  - [x] 3.3 Write property test for tracing-disabled behaviour
    - **Property 23: Tracing-disabled performs no span creation or store writes**
    - **Validates: Requirements 10.1**

- [x] 4. Span recorder and stage attributes
  - [x] 4.1 Implement `SpanRecorder` trace and span lifecycle
    - Implement `start_trace` (adopt active trace_id or generate unique 32-hex id, Root_Span) and
      `record_span` (child span, timing, status, metrics, error capture + re-raise)
    - Catch span-creation failures, log them, and continue with a no-op `Span` sentinel
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 4.1, 4.2, 4.3, 4.4_

  - [x] 4.2 Implement stage attribute helpers and scalar coercion
    - Implement `set_attributes` (coerce any non-scalar to its string representation) and stage
      helpers for generation/routing, retrieval, answer-generation, document-id, and ingestion stages
      including the "unavailable" / "no score" sentinels
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 12.4, 12.5_

  - [x] 4.3 Write property test for root span trace id
    - **Property 1: Root span adopts or generates a valid unique trace id**
    - **Validates: Requirements 1.1, 1.2**

  - [x] 4.4 Write property test for span hierarchy
    - **Property 2: Span hierarchy reflects call nesting**
    - **Validates: Requirements 1.3, 1.6**

  - [x] 4.5 Write property test for span lifecycle duration and status
    - **Property 3: Span lifecycle records non-negative integer duration and correct status**
    - **Validates: Requirements 1.4, 1.5, 4.1, 4.3, 12.7**

  - [x] 4.6 Write property test for exception capture
    - **Property 4: Exceptions are recorded and re-raised unchanged with bounded message**
    - **Validates: Requirements 4.2, 4.4**

  - [x] 4.7 Write property test for attribute scalar coercion
    - **Property 5: Span attribute values are always scalar**
    - **Validates: Requirements 3.1, 3.3, 3.5, 3.6, 3.7**

  - [x] 4.8 Write unit tests for attribute sentinels
    - Test missing model/token sentinels (R3.2) and the `hit_count == 0` no-score path (R3.4)
    - _Requirements: 3.2, 3.4_

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Bounded in-memory buffers
  - [x] 6.1 Implement `BoundedSpanBuffer` and `BoundedLogBuffer`
    - Thread-safe ring buffers capped at 10,000 entries; on overflow drop new entries and increment
      `rag_spans_dropped_total` / `rag_logs_dropped_total` without raising
    - _Requirements: 9.4, 9.5, 17.3, 17.4_

  - [x] 6.2 Write property test for bounded buffer overflow
    - **Property 19: Bounded buffer caps size and counts drops on overflow**
    - **Validates: Requirements 9.4, 9.5, 17.3, 17.4**

- [x] 7. Serialization
  - [x] 7.1 Implement `TraceSerializer`
    - `serialize` produces `StoredTrace` with no field omitted/added/altered; `deserialize` rebuilds a
      full `Trace`; raise `TraceDeserializationError(reason, trace_id)` on malformed input and a generic
      failure when neither reason nor trace_id can be determined; raise on serialization failure with
      the affected trace_id and write nothing partial
    - _Requirements: 6.1, 6.2, 6.4, 6.5, 6.6_

  - [x] 7.2 Implement `LogSerializer`
    - Serialize/deserialize `LogRecordModel` retaining timestamp (UTC), level, logger, message,
      trace_id (explicit null when absent), exc_text, and extra
    - _Requirements: 14.1, 14.2, 14.3_

  - [x] 7.3 Write property test for trace round-trip
    - **Property 6: Trace serialization round-trip is identity**
    - **Validates: Requirements 5.2, 5.3, 6.1, 6.2, 6.3**

  - [x] 7.4 Write property test for malformed stored traces
    - **Property 7: Malformed stored traces fail cleanly**
    - **Validates: Requirements 6.4**

  - [x] 7.5 Write property test for log round-trip
    - **Property 18: Log serialization round-trip preserves all fields**
    - **Validates: Requirements 14.1, 14.2**

  - [x] 7.6 Write unit test for generic deserialization failure
    - Test the generic failure when trace_id/reason cannot be determined
    - _Requirements: 6.5_

- [x] 8. PostgreSQL schema and in-memory store double
  - [x] 8.1 Create schema DDL module
    - Define `traces`, `spans` (FK `ON DELETE CASCADE`), and `log_records` tables plus indexes for
      ordering/filtering, reusing `COPILOT_DB_*` connection settings
    - _Requirements: 5.2, 5.3, 13.2, 14.2, 14.3, 15.2_

  - [x] 8.2 Implement in-memory transactional store double for tests
    - Staging-area insert with atomic commit/rollback so atomicity and retention properties run without
      a live database
    - _Requirements: 5.1, 5.5_

- [x] 9. Trace store (PostgreSQL)
  - [x] 9.1 Implement `PostgresTraceStore.persist`
    - Single atomic transaction inserting the trace row and all span rows; roll back fully on any
      failure; on failure log at WARNING (wrapped to survive a failing warning call), discard, increment
      `rag_trace_store_write_failures_total`; increment `rag_traces_persisted_total{route}` only after
      commit
    - _Requirements: 5.1, 5.4, 5.5, 5.6, 10.3_

  - [x] 9.2 Implement `PostgresTraceStore.get_trace` and `search_traces`
    - `get_trace` returns the trace with spans ordered by start ts then span_id, parent null for root;
      `search_traces` applies inclusive time range, case-sensitive route/status, min-duration, AND
      semantics, default limit 100 / max 1000, descending by start ts
    - _Requirements: 7.1, 7.2, 7.5, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.10_

  - [x] 9.3 Implement `PostgresTraceStore.enforce_retention`
    - Delete traces strictly older than the configured period (cascade to spans); retain boundary rows;
      retain everything when no period configured; on per-row failure retain the row and record an error
    - _Requirements: 13.1, 13.2, 13.3, 13.5_

  - [x] 9.4 Write property test for atomic persistence and commit counting
    - **Property 8: Trace persistence is atomic and counts only on commit**
    - **Validates: Requirements 5.1, 5.5, 5.6**

  - [x] 9.5 Write property test for trace retrieval
    - **Property 9: Trace retrieval returns the full, hierarchy-reconstructable span set**
    - **Validates: Requirements 7.1, 7.2, 7.5**

  - [x] 9.6 Write property test for trace search filters
    - **Property 11: Trace search returns exactly the traces satisfying every filter**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**

  - [x] 9.7 Write property test for trace search ordering and limit
    - **Property 12: Trace search ordering and limit are honoured**
    - **Validates: Requirements 8.6, 8.7**

  - [x] 9.8 Write property test for trace retention
    - **Property 32: Retention removes strictly-older entries and cascades to spans**
    - **Validates: Requirements 13.1, 13.2, 18.1**

  - [x] 9.9 Write unit tests for best-effort trace discard paths
    - Test WARNING-then-discard on persist failure, dropped-write metric, and no-period retention
    - _Requirements: 5.4, 10.3, 13.3, 13.5_

- [x] 10. Log store (PostgreSQL)
  - [x] 10.1 Implement `PostgresLogStore` persist, get-by-trace, and search
    - Persist log records (explicit null trace_id allowed); `get_by_trace` orders by ts desc with ties
      by insertion order desc; `search` applies inclusive range, case-sensitive level/trace_id, AND,
      default 100 / max 1000 desc
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 15.1, 15.2, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.9_

  - [x] 10.2 Implement `PostgresLogStore.enforce_retention`
    - Delete records strictly older than the period; retain boundary rows; retain all when no period;
      on per-row failure retain and record an error
    - _Requirements: 18.1, 18.2, 18.4_

  - [x] 10.3 Write property test for log search filters
    - **Property 14: Log search returns exactly the records satisfying every filter**
    - **Validates: Requirements 16.1, 16.2, 16.3, 16.4**

  - [x] 10.4 Write property test for log search ordering and limit
    - **Property 15: Log search ordering and limit are honoured**
    - **Validates: Requirements 16.5, 16.6**

  - [x] 10.5 Write property test for log retrieval by trace id
    - **Property 17: Log retrieval by trace id returns all matching records in tie-broken order**
    - **Validates: Requirements 15.1, 15.2**

  - [x] 10.6 Write unit tests for log discard and no-period retention
    - Test WARNING-then-discard on persist failure (R14.5) and retain-all when no period (R18.2)
    - _Requirements: 14.5, 18.2, 18.4_

- [x] 11. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Background flush workers
  - [x] 12.1 Implement `TraceFlushWorker` and `LogFlushWorker`
    - Daemon-thread batch loops draining buffers (spans grouped by trace) into the stores off the
      request path; drain buffered entries within 30 s after store recovery
    - _Requirements: 9.1, 9.6, 17.1, 17.5_

  - [x] 12.2 Write property test for off-request-path persistence
    - **Property 20: Persistence happens only off the request path**
    - **Validates: Requirements 9.1, 17.1**

- [x] 13. Log capture handler
  - [x] 13.1 Implement `TracePersistingLogHandler`
    - Logging handler attached next to the existing StreamHandler that builds a `LogRecordModel` and
      enqueues it without blocking; the existing JSON line is still emitted
    - _Requirements: 14.1, 17.6_

  - [x] 13.2 Write property test for structured JSON log preservation
    - **Property 26: Structured JSON log line preserves all fields plus trace correlation**
    - **Validates: Requirements 11.1**

  - [x] 13.3 Write property test for log stream pass-through
    - **Property 33: Captured logs are still emitted to the existing log stream**
    - **Validates: Requirements 17.6**

- [x] 14. Query API endpoints
  - [x] 14.1 Implement `Trace_Query_Service` endpoints
    - Register `GET /traces/{trace_id}` (400 on bad 32-hex format, 404 when absent) and `GET /traces`
      (400 on inverted range or out-of-range limit/min-duration) on the existing app
    - _Requirements: 7.3, 7.4, 8.8, 8.9_

  - [x] 14.2 Implement `Log_Query_Service` endpoints
    - Register `GET /logs/{trace_id}` (400 on bad format, empty 200 when none) and `GET /logs`
      (400 on inverted range or out-of-range limit)
    - _Requirements: 15.3, 15.4, 16.7, 16.8, 16.9_

  - [x] 14.3 Write property test for trace_id path validation
    - **Property 10: trace_id path validation rejects non-conforming identifiers**
    - **Validates: Requirements 7.3, 15.3**

  - [x] 14.4 Write property test for trace search request validation
    - **Property 13: Trace search rejects invalid range and out-of-range parameters**
    - **Validates: Requirements 8.8, 8.9**

  - [x] 14.5 Write property test for log search request validation
    - **Property 16: Log search rejects invalid range and out-of-range limit**
    - **Validates: Requirements 16.7, 16.8**

  - [x] 14.6 Write unit tests for not-found and empty-result responses
    - Test 404 (R7.4) and empty-200 (R8.10, R15.4, R16.9) responses
    - _Requirements: 7.4, 8.10, 15.4, 16.9_

- [x] 15. HTTP middleware integration (Root_Span)
  - [x] 15.1 Extend `log_requests` middleware to open/close the Root_Span
    - Call the sampler, open `start_trace(route=...)`, record `http.status_code`, set status
      success/error (500 when undetermined), re-raise; preserve `X-Trace-Id` response header behaviour
    - _Requirements: 4.5, 4.6, 11.3, 11.4_

  - [x] 15.2 Write property test for X-Trace-Id round-trip
    - **Property 25: X-Trace-Id response header round-trips the request value**
    - **Validates: Requirements 11.3**

  - [x] 15.3 Write unit test for X-Trace-Id generation path
    - Test trace_id generation and response header when no header is supplied
    - _Requirements: 11.4_

- [x] 16. Pipeline stage instrumentation
  - [x] 16.1 Replace `timed()` call sites in `service.py` with `record_span`
    - Wrap each pipeline stage in `record_span` using the same operation labels and attach stage
      attributes (model/tokens, retrieval mode/hit count/top score, evidence status/citation count,
      document id); continue emitting `rag_operation_total` / `rag_operation_duration_ms`
    - _Requirements: 1.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 11.5, 11.6_

  - [x] 16.2 Propagate trace context across hybrid concurrent branches in `router.py`
    - Wrap the `ThreadPoolExecutor` branch callables with `propagate_into_thread` so spans attach to the
      originating trace
    - _Requirements: 2.3, 2.6_

  - [x] 16.3 Write property test for operation metrics parity
    - **Property 24: Operation metrics mirror the timed helper**
    - **Validates: Requirements 11.5, 11.6**

  - [x] 16.4 Write property test for concurrent branch attachment
    - **Property 30: Concurrent branches attach spans to the originating trace**
    - **Validates: Requirements 2.3, 2.6**

  - [x] 16.5 Write `/metrics` compatibility snapshot test
    - Assert every pre-existing metric name and label set is still present
    - _Requirements: 11.2_

- [x] 17. Ingestion pipeline tracing
  - [x] 17.1 Include the active trace_id in enqueued ingestion job payloads
    - Set the job payload `trace_id` to the active trace_id, or null when no trace is active
    - _Requirements: 2.7, 2.8_

  - [x] 17.2 Instrument the ingestion worker with Ingestion_Trace and stage spans
    - Open a Root_Span per job (adopt payload trace_id or generate a new independent id); create exactly
      one child span for parsing, chunking, embedding, and indexing with document id/version attributes;
      mark failing stage and root `error` on failure and each completed stage `success` otherwise;
      tolerate span-creation failures
    - _Requirements: 2.9, 2.10, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7_

  - [x] 17.3 Write property test for ingestion stage spans
    - **Property 27: Ingestion produces exactly one child span per stage with stage attributes**
    - **Validates: Requirements 12.2, 12.4**

  - [x] 17.4 Write property test for ingestion failure marking
    - **Property 28: Ingestion failure marks the failing stage and root as error**
    - **Validates: Requirements 12.6**

  - [x] 17.5 Write property test for enqueue-to-worker association
    - **Property 29: Enqueue-to-worker trace association is preserved**
    - **Validates: Requirements 2.7, 2.9, 2.10**

- [x] 18. Retention scheduler
  - [x] 18.1 Implement `RetentionScheduler`
    - Daemon-thread scheduler running at a configured interval (≤ 24 h) that invokes
      `Trace_Store.enforce_retention` and `Log_Store.enforce_retention`
    - _Requirements: 13.4, 18.3_

- [x] 19. Final wiring and integration
  - [x] 19.1 Wire the platform into application startup
    - Construct the propagator, sampler, recorder, buffers, stores, flush workers, log handler, query
      routers, and retention scheduler; attach the handler, register endpoints, and start background
      threads during app startup
    - _Requirements: 9.1, 17.1_

  - [x] 19.2 Write integration tests against live PostgreSQL
    - Persistence within 5,000 ms (R5.1), retrieval within 2,000 ms (R7.1, R15.1), buffer drain within
      30 s after outage (R9.6, R17.5), same-database correlation (R14.4)
    - _Requirements: 5.1, 7.1, 9.6, 14.4, 15.1, 17.5_

  - [x] 19.3 Write performance/smoke tests for latency budgets
    - ≤ 1 ms per span (R9.2) and ≤ 1 ms added when the store is down (R9.3, R17.2)
    - _Requirements: 9.2, 9.3, 17.2_

- [x] 20. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP.
- Each task references specific requirement clauses for traceability; property tests cite the design
  property they validate.
- Property tests use Hypothesis with a minimum of 100 iterations (`@settings(max_examples=100)`), one
  property-based test per correctness property, tagged
  `# Feature: ai-observability-platform, Property {number}: {property_text}`.
- Property tests run against the in-memory transactional store double and fakes; integration tests are
  gated behind a live-PostgreSQL marker.
- Checkpoints ensure incremental validation at natural breaks.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3"] },
    { "id": 1, "tasks": ["1.2", "2.1", "3.1", "6.1", "8.2"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "3.3", "4.1", "6.2", "8.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "7.1", "7.2"] },
    { "id": 4, "tasks": ["7.3", "7.4", "7.5", "7.6", "9.1", "10.1"] },
    { "id": 5, "tasks": ["9.2", "9.3", "10.2", "12.1", "13.1"] },
    { "id": 6, "tasks": ["9.4", "9.5", "9.6", "9.7", "9.8", "9.9", "10.3", "10.4", "10.5", "10.6", "12.2", "13.2", "13.3", "14.1", "14.2"] },
    { "id": 7, "tasks": ["14.3", "14.4", "14.5", "14.6", "15.1", "16.1", "17.1"] },
    { "id": 8, "tasks": ["15.2", "15.3", "16.2", "16.3", "16.4", "16.5", "17.2", "18.1"] },
    { "id": 9, "tasks": ["17.3", "17.4", "17.5", "19.1"] },
    { "id": 10, "tasks": ["19.2", "19.3"] }
  ]
}
```
