# Requirements Document

## Introduction

This feature adds an **end-to-end request tracing** capability to the existing production RAG
system (`src/rag_system/`). Today the system already emits structured JSON logs correlated by a
`trace_id` (propagated via a `ContextVar`), exposes an in-process Prometheus `/metrics` endpoint,
and persists a single `QueryTraceRecord` to S3 for the RAG `/query` path. What is missing is a
unified, hierarchical, queryable trace that spans an entire request across every pipeline stage —
HTTP entry, agentic routing/classification, dense embedding, hybrid retrieval, reranking, answer
generation, the database copilot SQL path, hybrid synthesis — and across the asynchronous worker
ingestion pipeline (parse → chunk → embed → index).

The tracing platform captures one **trace** per logical request, composed of nested **spans** for
each instrumented operation. Each span records timing, status, error information, and
stage-specific attributes (model identifiers, token usage, retrieval scores, citation counts).
Traces are written off the request path to a durable, queryable store backed by the existing
PostgreSQL database, and exposed through a query API so operators can search and inspect traces by
time, route, status, latency, and document. The capability must add negligible latency to live
requests and must not change existing logging or metrics behaviour.

This requirements document covers the request-tracing capability and a backend-served log-querying
capability. In addition to traces, the backend persists the existing structured JSON log records
into a queryable store and exposes them through a query API, so that a frontend Observability tab
can display logs alongside the spans of a trace without relying on an external log aggregator.
Dashboards, alerting, drift detection, and cost-management features are explicitly out of scope for
this iteration and may be addressed in follow-on specs.

## Glossary

- **Tracing_Platform**: The overall subsystem introduced by this feature that captures, stores, and
  exposes end-to-end request traces.
- **Trace**: A single record representing the complete processing of one logical request,
  identified by a `trace_id` and containing one or more spans.
- **Span**: A timed unit of work within a trace (for example, "dense retrieval" or "answer
  generation"), with a start time, duration, status, optional parent span, and attributes.
- **Root_Span**: The top-level span of a trace, representing the request entry point (an HTTP
  request or an ingestion job).
- **Span_Recorder**: The component responsible for creating, timing, annotating, and closing spans.
- **Trace_Context_Propagator**: The component responsible for carrying the active trace and span
  identity across function calls, threads, and the SQS ingestion-job boundary.
- **Trace_Store**: The durable, queryable persistence layer for traces and spans, backed by the
  existing PostgreSQL database (psycopg).
- **Trace_Query_Service**: The component and HTTP endpoints that retrieve and filter traces from the
  Trace_Store.
- **Trace_Serializer**: The component that converts a Trace to and from its stored representation.
- **Trace_Sampler**: The component that decides whether a given trace is recorded, based on
  configuration.
- **trace_id**: The existing string identifier used to correlate logs and traces; supplied via the
  `X-Trace-Id` request header or generated when absent.
- **span_id**: A unique identifier for a span within a trace.
- **Pipeline_Stage**: Any instrumented operation in the request or ingestion path (routing,
  embedding, sparse encoding, retrieval, reranking, generation, copilot SQL execution, hybrid
  synthesis, parsing, chunking, indexing).
- **Span_Status**: The terminal outcome of a span, one of `success` or `error`.
- **Ingestion_Trace**: A trace whose Root_Span represents a document ingestion job processed by the
  worker.
- **Log_Record**: A single structured JSON log entry emitted by the system, retaining every field
  described in Requirement 11 (including timestamp, level, logger name, message, optional exception
  text, and stage-specific extra fields) and correlated to a request by its trace_id.
- **Log_Level**: The severity classification of a Log_Record, one of the standard Python logging
  levels (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`).
- **Log_Store**: The durable, queryable persistence layer for Log_Records, backed by the existing
  PostgreSQL database (psycopg) consistent with the Trace_Store approach.
- **Log_Query_Service**: The component and HTTP endpoints that retrieve and filter Log_Records from
  the Log_Store.

## Requirements

### Requirement 1: Trace and span lifecycle

**User Story:** As an SRE, I want each request and each instrumented operation captured as a trace
with nested spans, so that I can see the full execution structure of a request.

#### Acceptance Criteria

1. WHEN a request begins processing and an active trace_id is set, THE Span_Recorder SHALL create a Trace containing a Root_Span identified by the active trace_id.
2. WHEN a request begins processing and no active trace_id is set, THE Span_Recorder SHALL generate a new trace_id that is unique among all currently active Traces and create a Trace containing a Root_Span identified by that trace_id.
3. WHEN an instrumented Pipeline_Stage begins, THE Span_Recorder SHALL create a Span with a span_id that is unique within its Trace, a recorded start timestamp, and a reference to the currently active span as its parent.
4. WHEN an instrumented Pipeline_Stage completes successfully, THE Span_Recorder SHALL record the Span duration as a non-negative value rounded to the nearest whole millisecond and set the Span_Status to `success`.
5. IF an instrumented Pipeline_Stage raises an exception, THEN THE Span_Recorder SHALL record the Span duration as a non-negative value rounded to the nearest whole millisecond and set the Span_Status to `error`.
6. WHEN a Span is closed, THE Span_Recorder SHALL restore the closed Span's parent reference as the active span for the current execution context.
7. THE Span_Recorder SHALL record the operation name for every Span using the same operation labels currently used by the `timed` helper.
8. IF Span creation fails, THEN THE Tracing_Platform SHALL emit an observable log entry indicating the Span creation failure and SHALL allow the Pipeline_Stage to continue executing without a Span and without propagating an exception.

### Requirement 2: Trace context propagation

**User Story:** As a developer, I want trace context to follow execution across threads and the
ingestion queue, so that all work for one request appears in a single trace.

#### Acceptance Criteria

1. THE Trace_Context_Propagator SHALL expose the active trace_id and active span_id through a context variable consistent with the existing `rag_trace_id` context variable.
2. WHERE no Trace is active, THE Trace_Context_Propagator SHALL resolve the active trace_id and active span_id to a null value.
3. WHEN work is dispatched to a background thread, THE Trace_Context_Propagator SHALL make the originating trace_id and active span_id available to that thread before the dispatched work begins executing.
4. WHEN background work dispatched to a thread completes, THE Trace_Context_Propagator SHALL restore the thread's previous trace context so that the propagated trace_id and span_id do not leak to subsequent work on a pooled thread.
5. IF trace-context propagation to a background thread fails, THEN THE Tracing_Platform SHALL resolve the active trace_id and active span_id to null within that thread, record an error indication, and allow the background work to proceed.
6. WHEN a hybrid query runs the RAG and copilot branches concurrently, THE Span_Recorder SHALL attach every Span created in each branch to the Trace identified by the originating trace_id.
7. WHEN an ingestion job is enqueued while a Trace is active, THE Tracing_Platform SHALL include the active trace_id in the job payload.
8. WHEN an ingestion job is enqueued while no Trace is active, THE Tracing_Platform SHALL set the job payload trace_id to a null value.
9. WHEN the worker processes an ingestion job whose payload carries a non-null trace_id, THE Tracing_Platform SHALL associate the resulting Ingestion_Trace with that trace_id.
10. IF an ingestion job carries a null or absent trace_id, THEN THE Tracing_Platform SHALL generate a new trace_id for the Ingestion_Trace as an independent Trace with no parent linkage.

### Requirement 3: Span attributes

**User Story:** As an analyst, I want each span annotated with stage-specific data, so that I can
diagnose quality and performance without reading raw logs.

#### Acceptance Criteria

1. WHEN a generation Span or routing Span completes, THE Span_Recorder SHALL record, as Span attributes, the model identifier as a string and the token usage counts (prompt token count, completion token count, and total token count) as non-negative integers returned by the LLM provider.
2. IF a generation Span or routing Span completes and the LLM provider does not return a model identifier or one or more token usage counts, THEN THE Span_Recorder SHALL record each absent value's attribute with an explicit value indicating that the data is unavailable and SHALL complete recording of all other available attributes without raising an error.
3. WHEN a retrieval Span completes, THE Span_Recorder SHALL record, as Span attributes, the retrieval mode as a string, the hit count as a non-negative integer, and the top retrieval score as a number.
4. IF a retrieval Span completes with a hit count of 0, THEN THE Span_Recorder SHALL record the top-retrieval-score attribute with an explicit value indicating that no score is available.
5. WHEN an answer-generation Span completes, THE Span_Recorder SHALL record, as Span attributes, the evidence status as a string and the citation count as a non-negative integer.
6. WHERE a Pipeline_Stage produces a document identifier, THE Span_Recorder SHALL record the document identifier as a string Span attribute regardless of whether the Pipeline_Stage succeeds or the document is valid.
7. THE Span_Recorder SHALL record every Span attribute using a value type limited to string, number, or boolean, and SHALL convert any value not natively of these types to its string representation before recording.

### Requirement 4: Error capture

**User Story:** As an on-call engineer, I want failed operations recorded on their span, so that I
can locate the failing stage of a request.

#### Acceptance Criteria

1. IF an exception propagates out of an instrumented Pipeline_Stage, THEN THE Span_Recorder SHALL set the Span_Status of that Span to `error`.
2. IF an exception propagates out of an instrumented Pipeline_Stage, THEN THE Span_Recorder SHALL record the exception type and exception message as Span attributes, truncating the exception message to a maximum of 4,096 characters.
3. IF an exception propagates out of an instrumented Pipeline_Stage, THEN THE Span_Recorder SHALL record the Span duration in milliseconds as a non-negative value rounded to the nearest integer.
4. IF an exception propagates out of an instrumented Pipeline_Stage, THEN THE Span_Recorder SHALL re-raise the original exception unchanged after recording the Span_Status, exception attributes, and Span duration.
5. WHEN an HTTP request handler raises an unhandled exception, THE Span_Recorder SHALL set the Root_Span status to `error` and record the HTTP response status code returned to the caller as a Root_Span attribute.
6. IF an unhandled exception occurs before an HTTP response status code has been determined, THEN THE Span_Recorder SHALL record the Root_Span response status code attribute as `500`.

### Requirement 5: Durable trace persistence

**User Story:** As an operator, I want traces stored durably and queryably, so that I can
investigate requests after they complete.

#### Acceptance Criteria

1. WHEN a Trace is completed, THE Trace_Store SHALL persist the Trace and all of its spans to the PostgreSQL database within a single atomic transaction, completing the write within 5000 milliseconds of Trace completion.
2. THE Trace_Store SHALL persist for each Trace the trace_id, the request route, the start timestamp recorded in UTC, the total duration as a non-negative integer in milliseconds, and the terminal Span_Status of the Root_Span.
3. THE Trace_Store SHALL persist for each Span the span_id, the parent span_id (null for the Root_Span), the operation name, the start timestamp recorded in UTC, the duration as a non-negative integer in milliseconds, the Span_Status, and the Span attributes.
4. IF persistence of a Trace fails, THEN THE Tracing_Platform SHALL log the failure at WARNING level, SHALL discard the Trace so that no partial Trace or Span data remains in the database, and SHALL allow the originating request to return its normal response, including when the warning logging itself fails.
5. IF persistence of any Span within a Trace fails, THEN THE Trace_Store SHALL roll back the entire transaction so that neither the Trace nor any of its Spans are persisted.
6. WHEN a Trace is persisted and its transaction fully commits, THE Trace_Store SHALL increment by exactly one a metric counting persisted traces, labelled by route.

### Requirement 6: Trace serialization round-trip

**User Story:** As a developer, I want stored traces to deserialize back into equivalent trace
objects, so that retrieval and inspection are reliable.

#### Acceptance Criteria

1. WHEN the Trace_Serializer receives a valid Trace, THE Trace_Serializer SHALL serialize the Trace, including all spans and all span attributes, into the Trace_Store representation with no span, span attribute, or attribute value omitted, added, or altered.
2. WHEN the Trace_Serializer receives a stored Trace representation that is well-formed, THE Trace_Serializer SHALL deserialize it into a Trace object containing every span and span attribute present in the stored representation.
3. FOR ALL valid Traces, serializing a Trace and then deserializing the result SHALL produce a Trace equivalent to the original, where equivalence requires identical values for trace_id, route, the complete set of spans (matched by span identifier), each span's parent relationship, each span's duration, each span's status, and each span's complete set of attribute key-value pairs (round-trip property).
4. IF a stored Trace representation is malformed, where malformed means it cannot be parsed into the expected Trace structure or is missing a required field (trace_id, span identifier, span parent reference, duration, or status), THEN THE Trace_Serializer SHALL return an error that indicates the malformation reason and includes the affected trace_id, AND SHALL NOT return a partially deserialized Trace object.
5. IF the Trace_Serializer cannot determine the affected trace_id or the specific malformation reason, THEN THE Trace_Serializer SHALL return a generic failure error response that indicates deserialization failed.
6. IF serialization of a Trace fails, THEN THE Trace_Serializer SHALL return an error indicating the serialization failure with the affected trace_id, AND SHALL NOT write a partial Trace representation to the Trace_Store.

### Requirement 7: Trace retrieval by identifier

**User Story:** As an operator, I want to fetch a complete trace by its identifier, so that I can
inspect every stage of a specific request.

#### Acceptance Criteria

1. WHEN a client requests a trace by a syntactically valid trace_id that exists, THE Trace_Query_Service SHALL return the matching Trace together with all of its Spans (from 1 up to a maximum of 10,000 Spans) within 2,000 milliseconds.
2. WHEN a Trace is returned, THE Trace_Query_Service SHALL order its Spans in ascending order of Span start timestamp, and SHALL break ties between Spans sharing an identical start timestamp by ascending span_id.
3. IF the requested trace_id does not match the required format (a non-empty 32-character lowercase hexadecimal string), THEN THE Trace_Query_Service SHALL reject the request with HTTP status 400 and an error response indicating the trace_id is malformed, without returning any Trace data.
4. IF no Trace exists for a syntactically valid requested trace_id, THEN THE Trace_Query_Service SHALL return a not-found response using HTTP status 404 exclusively.
5. WHEN a Trace is returned, THE Trace_Query_Service SHALL include each Span's parent span_id, and SHALL set the parent span_id to an explicit null value for any Span that is the root of the Trace, so that clients can reconstruct the span hierarchy.

### Requirement 8: Trace search and filtering

**User Story:** As an analyst, I want to search traces by time, route, status, and latency, so that
I can find requests that match an investigation.

#### Acceptance Criteria

1. WHEN a client requests traces within a start and end timestamp range, THE Trace_Query_Service SHALL return only Traces whose start timestamp is greater than or equal to the supplied start timestamp and less than or equal to the supplied end timestamp, treating both boundaries as inclusive.
2. WHERE a client supplies a route filter, THE Trace_Query_Service SHALL return only Traces whose route value is exactly equal to the supplied value using a case-sensitive comparison.
3. WHERE a client supplies a status filter, THE Trace_Query_Service SHALL return only Traces whose Root_Span status is exactly equal to the supplied value using a case-sensitive comparison.
4. WHERE a client supplies a minimum-duration filter, THE Trace_Query_Service SHALL return only Traces whose total duration in milliseconds is greater than or equal to the supplied value, where the supplied value is an integer in the range 0 to 86,400,000 inclusive.
5. WHEN a search request supplies two or more of the time-range, route, status, and minimum-duration filters, THE Trace_Query_Service SHALL return only Traces that satisfy every supplied filter simultaneously.
6. WHEN a search request omits a result limit, THE Trace_Query_Service SHALL return at most 100 Traces ordered by start timestamp in descending order.
7. WHERE a client supplies a result limit, THE Trace_Query_Service SHALL return at most the supplied number of Traces ordered by start timestamp in descending order, where the supplied limit is an integer in the range 1 to 1000 inclusive.
8. IF a search request supplies an end timestamp earlier than its start timestamp, THEN THE Trace_Query_Service SHALL reject the request without returning Traces and SHALL return a validation error with HTTP status 400 indicating the timestamp range is invalid.
9. IF a search request supplies a result limit outside the range 1 to 1000 or a minimum-duration value outside the range 0 to 86,400,000, THEN THE Trace_Query_Service SHALL reject the request without returning Traces and SHALL return a validation error with HTTP status 400 indicating which parameter is out of range.
10. WHEN a search request is valid but no Traces satisfy the supplied filters, THE Trace_Query_Service SHALL return an empty result set with HTTP status 200.

### Requirement 9: Low-overhead, non-blocking capture

**User Story:** As a product owner, I want tracing to avoid slowing user requests, so that
observability does not degrade the user experience.

#### Acceptance Criteria

1. THE Tracing_Platform SHALL perform all Trace_Store writes outside the request-response path of the originating request, performing no synchronous Trace_Store writes during request processing.
2. WHEN in-process span recording is performed for a single Span, THE Span_Recorder SHALL add no more than 1 millisecond of processing time per Span, measured from recording start to recording completion and excluding persistence.
3. IF the Trace_Store is unavailable, THEN THE Tracing_Platform SHALL continue serving requests and SHALL return the active query path response unchanged, adding no more than 1 millisecond of processing time to the request-response path.
4. WHILE the Trace_Store is unavailable, THE Tracing_Platform SHALL buffer recorded Spans in an in-memory buffer holding up to 10,000 Spans.
5. IF the in-memory Span buffer reaches its maximum capacity of 10,000 Spans, THEN THE Tracing_Platform SHALL discard newly recorded Spans and SHALL increment a dropped-Span counter, without raising an error to the originating request.
6. WHEN the Trace_Store becomes available after a period of unavailability, THE Tracing_Platform SHALL write all buffered Spans to the Trace_Store within 30 seconds.

### Requirement 10: Sampling and configuration

**User Story:** As an operator, I want to control whether and how much tracing is captured, so that
I can manage storage and overhead.

#### Acceptance Criteria

1. WHERE tracing is disabled by configuration, THE Tracing_Platform SHALL skip Span creation and SHALL perform zero Trace_Store writes.
2. WHILE tracing is enabled and the Trace_Store is unavailable, THE Span_Recorder SHALL continue to create Spans and SHALL allow the originating request to complete without raising an error to the request path.
3. IF tracing is enabled and a Trace_Store write fails or the Trace_Store is unavailable, THEN THE Span_Recorder SHALL discard the affected Span write and SHALL record a metric indicating a dropped Trace_Store write.
4. WHERE a trace sampling rate between 0.0 and 1.0 inclusive is configured, THE Trace_Sampler SHALL record a proportion of Traces equal to the configured rate within a tolerance of plus or minus 5 percent measured over 1000 traces.
5. WHERE no trace sampling rate is configured, THE Trace_Sampler SHALL apply a default sampling rate of 1.0, recording all Traces.
6. IF a configured trace sampling rate is non-numeric or outside the range 0.0 to 1.0 inclusive, THEN THE Tracing_Platform SHALL reject the configuration at startup and SHALL emit an error indicating the invalid sampling rate value.
7. WHILE tracing is enabled, WHEN a request arrives with an `X-Trace-Id` header, THE Trace_Sampler SHALL record that Trace regardless of the configured sampling rate.
8. WHERE tracing is disabled by configuration, THE Trace_Sampler SHALL ignore the `X-Trace-Id` header and SHALL treat the request as not sampled.
9. THE Tracing_Platform SHALL read tracing configuration from environment variables using the existing pydantic settings mechanism.

### Requirement 11: Compatibility with existing observability

**User Story:** As a maintainer, I want the new tracing layer to coexist with current logging and
metrics, so that existing dashboards and operational tooling keep working.

#### Acceptance Criteria

1. WHEN structured JSON logging is enabled and a log record is emitted, THE Tracing_Platform SHALL output that record as a single-line JSON object that retains every field present before the tracing layer was added and includes the trace_id correlation field set to the active trace identifier.
2. WHEN a client requests the `/metrics` endpoint, THE Tracing_Platform SHALL return the Prometheus text exposition output containing every metric name and label set that was exposed before the tracing layer was added.
3. WHEN a response is returned and the originating request included an `X-Trace-Id` header, THE Tracing_Platform SHALL set the `X-Trace-Id` response header to the same trace_id value received on the request.
4. IF a request is received without an `X-Trace-Id` header, THEN THE Tracing_Platform SHALL generate a trace_id for the request and set it in the `X-Trace-Id` response header.
5. WHEN an instrumented operation completes successfully, THE Tracing_Platform SHALL emit the operation duration metric and operation count for that operation labeled with a success status, as currently emitted by the `timed` helper.
6. IF an instrumented operation fails, THEN THE Tracing_Platform SHALL emit the operation duration metric and operation count for that operation labeled with an error status, as currently emitted by the `timed` helper.

### Requirement 12: Ingestion pipeline tracing

**User Story:** As an SRE, I want document ingestion captured as traces, so that I can diagnose
failures in the asynchronous worker pipeline.

#### Acceptance Criteria

1. WHEN the worker begins processing an ingestion job, THE Span_Recorder SHALL create an Ingestion_Trace whose Root_Span represents the job, recording the Root_Span start timestamp at job start and the Root_Span end timestamp at job completion.
2. WHEN each ingestion stage runs, THE Span_Recorder SHALL create exactly one child Span per stage for parsing, chunking, embedding, and indexing, each as a direct child of the Root_Span with a recorded start timestamp and end timestamp.
3. IF Span creation fails while an ingestion stage succeeds, THEN THE Tracing_Platform SHALL allow the ingestion job to continue to completion without the corresponding Span and SHALL NOT propagate the span-creation failure as an error to the job.
4. WHEN an ingestion stage completes, THE Span_Recorder SHALL record the document identifier and document version as attributes of that stage's child Span.
5. IF the document identifier or document version is unavailable when an ingestion stage completes, THEN THE Span_Recorder SHALL record the corresponding attribute with an explicit value indicating the data is unavailable.
6. IF an ingestion job fails, THEN THE Span_Recorder SHALL set the Span_Status of the failing stage Span and of the Root_Span to `error` and SHALL record an error indication attribute on the failing stage Span.
7. WHILE an ingestion job is processing without failure, THE Span_Recorder SHALL set the Span_Status of each completed ingestion stage Span to `success`.

### Requirement 13: Trace retention

**User Story:** As an operator, I want old traces removed automatically, so that the Trace_Store
does not grow without bound.

#### Acceptance Criteria

1. WHERE a retention period between 1 hour and 3650 days is configured, WHEN a retention enforcement cycle executes, THE Trace_Store SHALL remove Traces whose age (current time minus the Trace start timestamp) is strictly greater than the configured retention period and SHALL retain Traces whose age is less than or equal to the configured retention period.
2. WHEN Traces are removed by retention, THE Trace_Store SHALL remove all spans belonging to the removed Traces within the same retention enforcement cycle.
3. WHERE no retention period is configured, THE Trace_Store SHALL retain all persisted Traces.
4. WHERE a retention period is configured, THE Trace_Store SHALL execute a retention enforcement cycle at a configured interval not exceeding 24 hours.
5. IF removal of a Trace or any of its spans fails during a retention enforcement cycle, THEN THE Trace_Store SHALL retain that Trace and its spans intact and SHALL record an error indication identifying the failed removal.

### Requirement 14: Durable log persistence

**User Story:** As an operator, I want the structured JSON log records persisted in a queryable
store, so that a frontend Observability tab can display logs without an external log aggregator.

#### Acceptance Criteria

1. WHEN a structured JSON Log_Record is emitted, THE Log_Store SHALL persist that Log_Record to the PostgreSQL database, retaining every field present in the emitted Log_Record as described in Requirement 11.
2. THE Log_Store SHALL persist for each Log_Record the record timestamp recorded in UTC, the Log_Level, the logger name, the message text, and the trace_id correlation value.
3. WHERE an emitted Log_Record carries a null or absent trace_id, THE Log_Store SHALL persist the Log_Record with the trace_id stored as an explicit null value.
4. THE Log_Store SHALL persist each Log_Record using the existing PostgreSQL database consistent with the Trace_Store, correlating each Log_Record to its Trace by the trace_id value.
5. IF persistence of a Log_Record fails, THEN THE Tracing_Platform SHALL log the failure at WARNING level to the existing log stream and SHALL discard the affected Log_Record so that no partial Log_Record data remains in the database, including when the warning logging itself fails.

### Requirement 15: Log retrieval by trace identifier

**User Story:** As an operator, I want to fetch all log records for a specific trace_id, so that I
can view a request's logs alongside its spans in the Observability tab.

#### Acceptance Criteria

1. WHEN a client requests Log_Records by a syntactically valid trace_id that exists, THE Log_Query_Service SHALL return all Log_Records whose trace_id is exactly equal to the supplied value within 2,000 milliseconds.
2. WHEN Log_Records are returned for a trace_id, THE Log_Query_Service SHALL order the Log_Records by record timestamp in descending order, and SHALL break ties between Log_Records sharing an identical timestamp by descending insertion order.
3. IF the requested trace_id does not match the required format (a non-empty 32-character lowercase hexadecimal string), THEN THE Log_Query_Service SHALL reject the request with HTTP status 400 and an error response indicating the trace_id is malformed, without returning any Log_Record data.
4. WHEN a client requests Log_Records by a syntactically valid trace_id for which no Log_Records exist, THE Log_Query_Service SHALL return an empty result set with HTTP status 200.

### Requirement 16: Log search and filtering

**User Story:** As an analyst, I want to search logs by time, level, and trace_id, so that I can
find log records that match an investigation.

#### Acceptance Criteria

1. WHEN a client requests Log_Records within a start and end timestamp range, THE Log_Query_Service SHALL return only Log_Records whose record timestamp is greater than or equal to the supplied start timestamp and less than or equal to the supplied end timestamp, treating both boundaries as inclusive.
2. WHERE a client supplies a Log_Level filter, THE Log_Query_Service SHALL return only Log_Records whose Log_Level is exactly equal to the supplied value using a case-sensitive comparison.
3. WHERE a client supplies a trace_id filter, THE Log_Query_Service SHALL return only Log_Records whose trace_id is exactly equal to the supplied value using a case-sensitive comparison.
4. WHEN a search request supplies two or more of the time-range, Log_Level, and trace_id filters, THE Log_Query_Service SHALL return only Log_Records that satisfy every supplied filter simultaneously.
5. WHEN a search request omits a result limit, THE Log_Query_Service SHALL return at most 100 Log_Records ordered by record timestamp in descending order.
6. WHERE a client supplies a result limit, THE Log_Query_Service SHALL return at most the supplied number of Log_Records ordered by record timestamp in descending order, where the supplied limit is an integer in the range 1 to 1000 inclusive.
7. IF a search request supplies an end timestamp earlier than its start timestamp, THEN THE Log_Query_Service SHALL reject the request without returning Log_Records and SHALL return a validation error with HTTP status 400 indicating the timestamp range is invalid.
8. IF a search request supplies a result limit outside the range 1 to 1000, THEN THE Log_Query_Service SHALL reject the request without returning Log_Records and SHALL return a validation error with HTTP status 400 indicating the limit parameter is out of range.
9. WHEN a search request is valid but no Log_Records satisfy the supplied filters, THE Log_Query_Service SHALL return an empty result set with HTTP status 200.

### Requirement 17: Low-overhead, non-blocking log capture

**User Story:** As a product owner, I want log persistence to avoid slowing user requests, so that
log querying does not degrade the user experience.

#### Acceptance Criteria

1. THE Tracing_Platform SHALL perform all Log_Store writes outside the request-response path of the originating request, performing no synchronous Log_Store writes during request processing.
2. IF the Log_Store is unavailable, THEN THE Tracing_Platform SHALL continue serving requests and SHALL return the active query path response unchanged, adding no more than 1 millisecond of processing time to the request-response path.
3. WHILE the Log_Store is unavailable, THE Tracing_Platform SHALL buffer captured Log_Records in an in-memory buffer holding up to 10,000 Log_Records.
4. IF the in-memory Log_Record buffer reaches its maximum capacity of 10,000 Log_Records, THEN THE Tracing_Platform SHALL discard newly captured Log_Records and SHALL increment a dropped-Log_Record counter, without raising an error to the originating request.
5. WHEN the Log_Store becomes available after a period of unavailability, THE Tracing_Platform SHALL write all buffered Log_Records to the Log_Store within 30 seconds.
6. WHEN a Log_Record is captured for persistence, THE Tracing_Platform SHALL emit that Log_Record to the existing log stream as described in Requirement 11, so that the existing structured JSON log output is preserved.

### Requirement 18: Log retention

**User Story:** As an operator, I want old log records removed automatically, so that the Log_Store
does not grow without bound.

#### Acceptance Criteria

1. WHERE a log retention period between 1 hour and 3650 days is configured, WHEN a retention enforcement cycle executes, THE Log_Store SHALL remove Log_Records whose age (current time minus the Log_Record timestamp) is strictly greater than the configured log retention period and SHALL retain Log_Records whose age is less than or equal to the configured log retention period.
2. WHERE no log retention period is configured, THE Log_Store SHALL retain all persisted Log_Records.
3. WHERE a log retention period is configured, THE Log_Store SHALL execute a retention enforcement cycle at a configured interval not exceeding 24 hours.
4. IF removal of a Log_Record fails during a retention enforcement cycle, THEN THE Log_Store SHALL retain that Log_Record intact and SHALL record an error indication identifying the failed removal.
