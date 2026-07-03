# Requirements Document

## Introduction

This feature is a suite of trust, evaluation, and observability enhancements for an existing retrieval-augmented generation (RAG) product. The product consists of a Python backend at `src/rag_system` (FastAPI-style `api.py` plus `retrieval`, `rerank`, `router`, `generation`, `confidence`, `evaluation`, `observability`, `observability_tracing`, `storage`, `models`, `service`, `worker`, `queue`, `conversation`, and `copilot` modules) and a React + TypeScript frontend at `frontendkimchi/src` (pages, components, api clients, hooks).

The enhancements are grouped by the user's priority ordering (priorities 2 through 10; there is no priority 1 in this request). Each priority is captured as one or more requirements. The goals are to increase answer trustworthiness (claim-level evidence, clarification, abstention), to make the product operable at scale (corpus inventory and versioning, feedback review, configuration versioning), and to make AI quality measurable and improvable (evaluation system, replay lab, trace investigator, knowledge gap map).

All new backend behavior is expected to be covered by pytest/Hypothesis tests, and all new frontend behavior by Vitest + Testing Library + MSW tests, per the project's testing preference. See the "Testing Considerations" section.

## Glossary

- **RAG_System**: The overall retrieval-augmented generation product, comprising the backend services and the frontend application.
- **Backend_API**: The FastAPI-style HTTP service defined in `src/rag_system/api.py`.
- **Frontend_App**: The React + TypeScript application in `frontendkimchi/src`.
- **Answer**: A generated response returned by the RAG_System to a user question.
- **Claim**: A discrete factual assertion contained within an Answer, identified by a stable Claim_ID and associated with an answer-text span.
- **Claim_ID**: A stable identifier assigned to a Claim that remains constant for that Claim across reads of the same Answer.
- **Answer_Span**: The start and end character offsets, within the Answer text, that delimit the text of a Claim. Offsets are zero-based, with the start offset inclusive and the end offset exclusive.
- **Evidence_Item**: A specific document passage or database row that is evaluated as support for a Claim. Each Evidence_Item carries an exact quote (for a passage) or row field values (for a database row), the character offsets of that quote within its source, and the identifiers of the source Document and Document_Version.
- **Verification_Result**: The outcome of checking whether a specific Evidence_Item entails a specific Claim, one of `entails`, `does_not_entail`, or `undetermined`.
- **Evidence_Status**: A classification assigned to each Claim, one of `supported`, `partially_supported`, `unsupported`, or `verification_unavailable`.
- **Retrieval_Service**: The backend module (`retrieval`) responsible for fetching candidate passages.
- **Rerank_Service**: The backend module (`rerank`) responsible for reordering retrieved candidates.
- **Router_Service**: The backend module (`router`) responsible for selecting a query route (for example RAG or SQL).
- **Clarification_Prompt**: A single focused question returned instead of an Answer when a question is classified as ambiguous.
- **clarification_id**: A unique, unguessable identifier issued with a Clarification_Prompt that binds the prompt to its originating conversation turn, document scope, and expiry.
- **Conversation_Turn_ID**: An identifier of the conversation and turn within which a Clarification_Prompt was issued.
- **Clarification_Expiry**: The timestamp after which a clarification_id is no longer valid and any reply referencing it SHALL be rejected.
- **Abstention_Response**: A response returned instead of an Answer when the RAG_System lacks sufficient evidence, containing a reason_code and a missing-information description and no Answer content.
- **reason_code**: A structured, machine-readable code accompanying an Abstention_Response, one of `low_confidence`, `no_evidence`, `unsupported_claims`, `conflicting_evidence`, `sql_no_rows`, or `retrieval_below_threshold`.
- **Generation_Service**: The backend module (`generation`) responsible for producing an Answer.
- **Confidence_Service**: The backend module (`confidence`) responsible for scoring Answer confidence.
- **Evaluation_Service**: The backend module (`evaluation`) responsible for scoring RAG_System quality.
- **Observability_Service**: The backend module (`observability`) responsible for storing and exposing operational data.
- **Tracing_Service**: The backend module (`observability_tracing`) responsible for recording query Traces.
- **Trace**: A stored record of a single query execution, including inputs, intermediate steps, and outputs.
- **Corpus**: The complete set of Documents stored in the RAG_System backend.
- **Corpus_Snapshot**: An immutable record, identified by a corpus_snapshot_id, that captures the exact manifest of Documents and their Document_Versions used by a run. Once created, a Corpus_Snapshot cannot be modified.
- **corpus_snapshot_id**: The unique identifier of a Corpus_Snapshot.
- **Document**: A single ingested source item in the Corpus, having an owner and one or more versions.
- **Document_Version**: A specific ingested revision of a Document.
- **Active_Version**: The Document_Version currently used for retrieval for a given Document.
- **Ingestion_Event**: A recorded processing of a Document_Version into the Corpus.
- **Feedback_Item**: A user-submitted rating and optional comment attached to an Answer. Ratings are on a 1-to-5 scale.
- **Negative_Rating**: A Feedback_Item rating of 1 or 2 on the 1-to-5 scale.
- **Review_Status**: The review state of a Feedback_Item, one of `unreviewed`, `reviewed`, or `resolved`.
- **Failure_Category**: An operator-assigned classification of a Feedback_Item, one of `Missing knowledge`, `Retrieval failure`, `Wrong route`, `Unsupported answer`, `SQL problem`, or `Ambiguous question`.
- **Evaluation_Set**: A curated collection of benchmark cases used to measure RAG_System quality.
- **Benchmark_Case**: A single human-reviewed question-and-expected-outcome entry in the Evaluation_Set.
- **Relevance_Labels**: The relevance ground truth attached to a Benchmark_Case, consisting of relevant chunk identifiers, relevant Document identifiers, or human relevance judgments, used to compute retrieval metrics.
- **LLM_Judge**: An automated evaluation method that uses a language model to score faithfulness and relevance.
- **Replay_Run**: An asynchronous job that re-executes a previously asked question under a specified AI_Configuration_Version and Corpus_Snapshot.
- **Replay_Run_State**: The lifecycle state of a Replay_Run, one of `queued`, `running`, `completed`, `failed`, or `cancelled`.
- **AI_Configuration**: The versioned set of settings (prompt, model, output schema, router threshold, retrieval settings, reranker configuration) that governs an Answer.
- **AI_Configuration_Version**: A specific stored, immutable revision of an AI_Configuration. Once created, an AI_Configuration_Version cannot be modified.
- **Activation_Event**: An audit record created when an AI_Configuration_Version is activated or rolled back, capturing the acting Operator, the previous and selected AI_Configuration_Versions, a timestamp, and a reason.
- **SQL_Result_Fixture**: A stored capture of the rows returned by a SQL-route query at a point in time, used to reproduce historical SQL-route execution during replay.
- **Trace_Investigator**: The AI assistant within Observability that diagnoses unsuccessful queries.
- **Knowledge_Gap_Map**: The feature that clusters low-quality query outcomes into topics and recommends corpus improvements.
- **Operator**: An authenticated user with permission to review feedback, manage the Corpus, run evaluations, and configure the RAG_System.

## Requirements

### Requirement 1: Claim-Level Evidence Mapping (Priority 2)

**User Story:** As a user reading an Answer, I want each factual claim mapped to the exact supporting passage or row, so that I can trust and verify the response.

#### Acceptance Criteria

1. WHEN the Generation_Service produces an Answer that contains at least one factual statement, THE Generation_Service SHALL decompose the Answer into one or more Claims, where each Claim expresses exactly one factual statement, carries a stable Claim_ID, and carries an Answer_Span identifying the start and end character offsets of the Claim within the Answer text.
2. WHEN the Generation_Service produces a Claim, THE Generation_Service SHALL associate the Claim with between 0 and 100 Evidence_Items, where each Evidence_Item includes an exact quote for a document passage or the row field values for a database row, the start and end character offsets of that quote within its source, and the identifiers of the source Document and Document_Version.
3. WHEN the Generation_Service associates an Evidence_Item with a Claim, THE Generation_Service SHALL record a Verification_Result of `entails`, `does_not_entail`, or `undetermined` for that Claim-and-Evidence_Item pairing.
4. WHERE at least one Evidence_Item associated with a Claim has a Verification_Result of `entails` and that Evidence_Item entails the entire Claim, THE Generation_Service SHALL assign the Claim an Evidence_Status of `supported`.
5. WHERE some but not all sub-parts of a Claim are entailed by an associated Evidence_Item with a Verification_Result of `entails` and no single associated Evidence_Item entails the entire Claim, THE Generation_Service SHALL assign the Claim an Evidence_Status of `partially_supported`.
6. WHERE a Claim has at least one associated Evidence_Item and every associated Evidence_Item has a Verification_Result of `does_not_entail`, THE Generation_Service SHALL assign the Claim an Evidence_Status of `unsupported`.
7. WHERE a Claim is associated with zero Evidence_Items, THE Generation_Service SHALL assign the Claim an Evidence_Status of `unsupported`.
8. WHERE the Verification_Result for every associated Evidence_Item of a Claim is `undetermined`, THE Generation_Service SHALL assign the Claim an Evidence_Status of `verification_unavailable`.
9. IF the Generation_Service cannot decompose an Answer into Claims, THEN THE Generation_Service SHALL return the Answer with an empty Claims list and an indication that claim decomposition failed.
10. WHEN the Frontend_App displays an Answer, THE Frontend_App SHALL render each Claim with an indicator of the Claim's Evidence_Status that is distinct per status and does not rely on color alone.
11. WHEN a user selects a Claim whose Evidence_Status is `supported` or `partially_supported` in the Frontend_App, THE Frontend_App SHALL display the Evidence_Items associated with the selected Claim, including each Evidence_Item's quote or row field values, source Document, and Document_Version.
12. IF the Frontend_App cannot retrieve the Evidence_Items for a selected Claim, THEN THE Frontend_App SHALL indicate that the evidence is unavailable and SHALL preserve the displayed Answer and Claims.
13. WHERE a Claim has an Evidence_Status of `unsupported` or `verification_unavailable`, THE Frontend_App SHALL display a marker identifying that status that is distinct from the indicators used for `supported` and `partially_supported` Claims.
14. WHEN the Backend_API returns an Answer, THE Backend_API SHALL include each Claim with its Claim_ID and Answer_Span, its associated Evidence_Items with their Verification_Results, and exactly one Evidence_Status per Claim from the set {`supported`, `partially_supported`, `unsupported`, `verification_unavailable`} in the response body.

### Requirement 2: Ambiguity Clarification (Priority 3)

**User Story:** As a user asking an ambiguous question, I want the system to ask one focused clarifying question, so that I receive an accurate answer instead of a guess.

#### Acceptance Criteria

1. WHEN the Backend_API receives a question that the Router_Service classifies as ambiguous, THE Backend_API SHALL return a single Clarification_Prompt instead of an Answer.
2. WHEN the Backend_API returns a Clarification_Prompt, THE Backend_API SHALL include exactly one clarification question, a unique clarification_id, the Conversation_Turn_ID of the originating turn, a Clarification_Expiry, and the document scope associated with the clarification in the response.
3. WHEN the Frontend_App receives a Clarification_Prompt, THE Frontend_App SHALL display the clarification question and accept a user reply.
4. WHEN the user submits a non-empty reply that references a valid, unexpired clarification_id, THE Backend_API SHALL process the original question combined with the clarification reply, scoped to the document scope associated with that clarification_id, and return an Answer.
5. IF the user submits a reply that references a clarification_id that is unknown, or whose Clarification_Expiry has passed, THEN THE Backend_API SHALL reject the reply and return an error indicating that the clarification is invalid or expired.
6. IF the user submits an empty or whitespace-only reply to a Clarification_Prompt, THEN THE Backend_API SHALL reject the reply and return an error indicating that a clarification reply is required.
7. WHILE processing a single original question, THE Backend_API SHALL return at most one Clarification_Prompt.
8. IF, after the one permitted clarification for an original question, the RAG_System still cannot resolve the ambiguity, THEN THE Backend_API SHALL return an Abstention_Response and SHALL NOT return a further Clarification_Prompt.
9. IF the RAG_System cannot determine which of multiple document scopes to search, THEN THE Backend_API SHALL request clarification of whether to search the selected Documents or the entire Corpus.

### Requirement 3: Evidence-Based Abstention (Priority 3)

**User Story:** As a user, I want the system to tell me when it lacks sufficient evidence, so that I am not misled by an unsupported answer.

#### Acceptance Criteria

1. IF the Confidence_Service assigns a confidence score below the configured minimum confidence threshold defined for the selected route, THEN THE Backend_API SHALL return an Abstention_Response instead of an Answer, with a reason_code of `low_confidence`.
2. IF the Retrieval_Service returns no retrieved evidence for a question, THEN THE Backend_API SHALL return an Abstention_Response with a reason_code of `no_evidence`.
3. IF an Answer would contain one or more material Claims with an Evidence_Status of `unsupported`, THEN THE Backend_API SHALL return an Abstention_Response with a reason_code of `unsupported_claims`.
4. IF the retrieved evidence for a question contains conflicting evidence, THEN THE Backend_API SHALL return an Abstention_Response with a reason_code of `conflicting_evidence`.
5. IF the selected route is the SQL route and the executed query returns no applicable rows, THEN THE Backend_API SHALL return an Abstention_Response with a reason_code of `sql_no_rows`.
6. IF every retrieval score for a question is below the configured retrieval score threshold, THEN THE Backend_API SHALL return an Abstention_Response with a reason_code of `retrieval_below_threshold`.
7. WHEN the Backend_API returns an Abstention_Response, THE Backend_API SHALL exclude all Answer content, including Claims and Evidence_Items, from the response.
8. WHEN the Backend_API returns an Abstention_Response, THE Backend_API SHALL include exactly one reason_code and a missing-information description of 1 to 1000 characters that identifies the aspects of the question that lack supporting evidence.
9. WHEN the Frontend_App receives an Abstention_Response, THE Frontend_App SHALL display the missing-information description and SHALL NOT display an Answer.
10. IF an Abstention_Response omits a missing-information description, THEN THE Frontend_App SHALL display a default insufficient-evidence notice.

### Requirement 4: Full Corpus Inventory (Priority 4)

**User Story:** As an Operator, I want the Documents page to show the complete backend Corpus, so that I can manage all documents regardless of which browser uploaded them.

#### Acceptance Criteria

1. WHEN an Operator opens the Documents page, THE Frontend_App SHALL send a Corpus listing request to the Backend_API.
2. WHEN the Backend_API receives a Corpus listing request from an authenticated Operator, THE Backend_API SHALL return a paginated page of the complete backend Corpus, regardless of which browser or client originally uploaded each Document, together with a next cursor when further Documents remain.
3. WHEN the Backend_API receives a Corpus listing request from an authenticated non-operator user, THE Backend_API SHALL return a paginated page containing only the Documents that user is authorized to access, together with a next cursor when further authorized Documents remain.
4. WHEN the Backend_API returns a Corpus listing page, THE Backend_API SHALL return no more than the configured page size of Documents and SHALL return a null next cursor when no further Documents remain.
5. WHEN a Corpus listing request includes a next cursor, THE Backend_API SHALL return the page of Documents that immediately follows the position identified by that cursor.
6. IF a Corpus listing request includes a cursor that is malformed or does not identify a valid position, THEN THE Backend_API SHALL reject the request and return an error indicating that the cursor is invalid, and the currently displayed Corpus listing SHALL remain unchanged.
7. WHEN an Operator selects a sort field of `name`, `owner`, or `date` and a sort direction on the Documents page, THE Backend_API SHALL return the Documents ordered by the selected sort field and direction, applied consistently across pages.
8. WHEN an Operator applies a filter on `status`, `owner`, `date`, or `active version` on the Documents page, THE Backend_API SHALL return only the Documents that satisfy the applied filter, paginated with a next cursor.
9. WHEN an Operator submits a search term of between 1 and 200 characters on the Documents page, THE Backend_API SHALL return the Documents whose metadata contains the search term, matched case-insensitively, paginated with a next cursor.
10. WHEN the Frontend_App receives a Corpus listing page, THE Frontend_App SHALL display each returned Document independently of Documents recorded in the current browser's local state.
11. THE Backend_API SHALL include the owner of each Document in the Corpus listing response.
12. IF a Corpus listing, search, or filter request to the Backend_API does not succeed, THEN THE Frontend_App SHALL display an error indicating that the Corpus could not be retrieved and SHALL retain the previously displayed Documents.
13. WHEN a Corpus listing, search, or filter request returns zero Documents, THE Frontend_App SHALL display an empty-state message indicating that no Documents match.
14. IF an Operator submits a search term exceeding 200 characters, THEN THE Backend_API SHALL reject the request and return an error indicating that the search term is too long, and the currently displayed Corpus listing SHALL remain unchanged.

### Requirement 5: Document Version Control (Priority 4)

**User Story:** As an Operator, I want document version control with ingestion history and restore, so that I can operate the AI reliably and recover from bad ingestions.

#### Acceptance Criteria

1. WHEN a Document is ingested successfully, THE Backend_API SHALL create a Document_Version and record an Ingestion_Event for the Document.
2. WHEN a Document is ingested successfully, THE Backend_API SHALL set the newly created Document_Version as the Active_Version of the Document.
3. IF ingestion of a Document does not complete successfully, THEN THE Backend_API SHALL create no new Document_Version, SHALL leave the current Active_Version unchanged, and SHALL record the failed Ingestion_Event.
4. THE Backend_API SHALL maintain at most one Active_Version per Document, and exactly one Active_Version for a non-deleted Document that has at least one successfully indexed Document_Version.
5. THE Backend_API SHALL retain the source content of every Document_Version, including superseded and non-active Document_Versions.
6. WHEN the Retrieval_Service retrieves passages for a Document, THE Retrieval_Service SHALL use the Active_Version of the Document.
7. WHEN an Operator requests the history of a Document, THE Backend_API SHALL return the Document's Document_Versions and Ingestion_Events ordered by ingestion timestamp, most recent first.
8. WHEN an Operator selects a previous Document_Version to restore and that Document_Version's indexed vectors still exist, THE Backend_API SHALL set the selected Document_Version as the Active_Version.
9. WHEN an Operator selects a previous Document_Version to restore and that Document_Version's indexed vectors have been cleaned up, THE Backend_API SHALL re-index the Document_Version from its retained source content and then set it as the Active_Version.
10. IF an Operator requests restoration of a Document_Version that does not exist for the Document, THEN THE Backend_API SHALL leave the Active_Version unchanged and return an error indicating the requested Document_Version was not found.
11. WHEN the Backend_API restores a previous Document_Version, THE Backend_API SHALL retain all prior Document_Versions of the Document.

### Requirement 6: Feedback Review Inbox (Priority 5)

**User Story:** As an Operator, I want an inbox of negative feedback with full context, so that I can classify failures and improve the system.

#### Acceptance Criteria

1. WHEN an Operator opens the feedback inbox, THE Backend_API SHALL return a paginated page of the Feedback_Items that have a Negative_Rating, ordered in reverse chronological order by submission time, together with a next cursor when further Feedback_Items remain, and SHALL return an empty collection when no Feedback_Item has a Negative_Rating.
2. WHEN the Backend_API returns a Feedback_Item, THE Backend_API SHALL include the rating, the comment, the expected answer, the confidence, the route, the retrieved chunks, the SQL, and the Review_Status associated with the Feedback_Item.
3. WHERE a Feedback_Item has no associated SQL, comment, or expected answer, THE Backend_API SHALL return an empty value for each absent field of that Feedback_Item.
4. WHEN an Operator filters the feedback inbox by a Review_Status of `unreviewed`, `reviewed`, or `resolved`, THE Backend_API SHALL return only the Feedback_Items whose Review_Status matches the selected value, paginated with a next cursor.
5. WHEN an Operator classifies a Feedback_Item with a Failure_Category that is one of `Missing knowledge`, `Retrieval failure`, `Wrong route`, `Unsupported answer`, `SQL problem`, or `Ambiguous question`, THE Backend_API SHALL persist the assigned Failure_Category with the Feedback_Item so that it is returned on subsequent reads, SHALL record the classifying Operator's identity and the review timestamp, and SHALL set the Review_Status to `reviewed`, replacing any previously assigned Failure_Category for that Feedback_Item.
6. WHEN an Operator promotes a reviewed Feedback_Item that has an expected answer into the Evaluation_Set, THE Backend_API SHALL create a Benchmark_Case in the Evaluation_Set derived from the promoted Feedback_Item's question and expected answer.
7. IF an Operator promotes a Feedback_Item that has no expected answer, THEN THE Backend_API SHALL not create a Benchmark_Case and SHALL return an error response indicating that an expected answer is required to promote the Feedback_Item.
8. WHEN an Operator marks a reviewed Feedback_Item as resolved, THE Backend_API SHALL set the Feedback_Item's Review_Status to `resolved` and SHALL continue to return the Feedback_Item in the inbox, filterable by Review_Status.
9. THE Frontend_App SHALL display each Feedback_Item in the inbox with the rating, comment, expected answer, confidence, route, retrieved chunks, SQL, and Review_Status.
10. IF an Operator submits a classification whose value is not one of the six defined Failure_Category values, THEN THE Backend_API SHALL reject the classification, SHALL leave the stored Failure_Category of the Feedback_Item unchanged, and SHALL return an error response indicating that the submitted value is not a valid Failure_Category.
11. IF an Operator promotes a Feedback_Item that has already been promoted into the Evaluation_Set, THEN THE Backend_API SHALL not create a duplicate Benchmark_Case and SHALL return an error response indicating that the Feedback_Item is already present in the Evaluation_Set.

### Requirement 7: Multi-Method Evaluation System (Priority 6)

**User Story:** As an Operator, I want a strong evaluation system combining deterministic, retrieval, LLM, and human methods, so that I can measure quality reliably.

#### Acceptance Criteria

1. WHEN the Evaluation_Service evaluates a Benchmark_Case, THE Evaluation_Service SHALL run deterministic checks that each produce a `pass` or `fail` outcome for citation presence, for the presence of each required fact defined by the Benchmark_Case, and for Evidence_Status correctness.
2. WHERE a Benchmark_Case carries Relevance_Labels, THE Evaluation_Service SHALL compute retrieval metrics for the case, including recall, precision, and mean reciprocal rank measured at the configured retrieval depth, against the Benchmark_Case's Relevance_Labels rather than against the expected answer alone.
3. WHERE LLM scoring is enabled, THE Evaluation_Service SHALL produce an LLM_Judge faithfulness score and an LLM_Judge relevance score for the Benchmark_Case, each expressed as a numeric value from 0.0 to 1.0 inclusive.
4. THE Evaluation_Set SHALL include at least one human-reviewed Benchmark_Case.
5. IF any deterministic check produces a `fail` outcome during a continuous integration run, THEN THE Evaluation_Service SHALL report a failing status for that continuous integration run.
6. THE Evaluation_Service SHALL run LLM_Judge scoring as a scheduled report at a configurable interval, and THE Evaluation_Service SHALL exclude LLM_Judge scores from the pass or fail determination of any continuous integration run.
7. WHEN the Evaluation_Service produces evaluation results, THE Evaluation_Service SHALL record the deterministic check outcomes, the retrieval metrics, and the LLM_Judge scores for each evaluated Benchmark_Case.
8. IF LLM scoring is enabled and LLM_Judge scoring for a Benchmark_Case does not complete within the configured scoring timeout of 60 seconds, THEN THE Evaluation_Service SHALL record an error indication in place of that Benchmark_Case's LLM_Judge scores and SHALL retain the recorded deterministic check outcomes and retrieval metrics for that Benchmark_Case.
9. WHERE a Benchmark_Case does not carry Relevance_Labels, THE Evaluation_Service SHALL NOT compute retrieval metrics for that Benchmark_Case.

### Requirement 8: Replay and Compare Lab (Priority 7)

**User Story:** As an Operator, I want to replay a question under different configurations and compare results side by side, so that I can make experimentation measurable.

#### Acceptance Criteria

1. WHEN an Operator initiates a Replay_Run for a question, THE Backend_API SHALL accept an approved AI_Configuration_Version identifier that specifies the prompt, the model, and the retrieval parameters (including the maximum number of retrieved passages, from 1 to 100, and the minimum retrieval score threshold, from 0.00 to 1.00), and a corpus_snapshot_id identifying the Corpus_Snapshot to use for the run.
2. WHEN the Backend_API accepts a Replay_Run, THE Backend_API SHALL create the Replay_Run in the `queued` state and return a Replay_Run identifier without waiting for the run to complete.
3. IF an Operator initiates a Replay_Run whose AI_Configuration_Version identifier does not reference an approved AI_Configuration_Version, or that specifies a prompt or model not drawn from an approved AI_Configuration_Version, THEN THE Backend_API SHALL reject the Replay_Run without executing it and return an error response indicating that an approved AI_Configuration_Version is required.
4. IF an Operator initiates a Replay_Run that omits a required setting (AI_Configuration_Version, retrieval parameters, or corpus_snapshot_id), specifies a value outside its permitted range, or references a corpus_snapshot_id that does not exist, THEN THE Backend_API SHALL reject the Replay_Run without executing it and return an error response indicating which setting is invalid.
5. WHEN the Backend_API begins executing a queued Replay_Run, THE Backend_API SHALL transition the Replay_Run to the `running` state and execute the specified question using the referenced AI_Configuration_Version against the referenced Corpus_Snapshot.
6. WHERE a Replay_Run uses the SQL route, THE Backend_API SHALL reproduce historical database results using a database snapshot or a stored SQL_Result_Fixture associated with the run rather than querying current data.
7. WHEN a Replay_Run finishes executing successfully, THE Backend_API SHALL transition the Replay_Run to the `completed` state and record the Answer, the supporting evidence, the route, the retrieval scores (each from 0.00 to 1.00), the latency in milliseconds, the token usage as prompt-token and completion-token counts, and the cost as a monetary amount for the run.
8. IF a Replay_Run fails during execution or exceeds the configured job timeout, THEN THE Backend_API SHALL transition the Replay_Run to the `failed` state, record the failure reason, and SHALL NOT record partial run results.
9. WHEN an Operator cancels a Replay_Run that is in the `queued` or `running` state, THE Backend_API SHALL transition the Replay_Run to the `cancelled` state and SHALL NOT record run results.
10. WHEN an Operator requests the status of a Replay_Run, THE Backend_API SHALL return the Replay_Run's current Replay_Run_State.
11. WHEN an Operator selects exactly two completed Replay_Runs to compare, THE Frontend_App SHALL display the Answer, supporting evidence, route, retrieval scores, latency in milliseconds, token usage, and cost of both Replay_Runs side by side.

### Requirement 9: Versioned AI Configuration (Priority 8)

**User Story:** As an Operator, I want every trace to record the exact AI configuration that produced it, with history and rollback, so that comparisons and reproductions are reliable.

#### Acceptance Criteria

1. WHEN the Tracing_Service records a Trace, THE Tracing_Service SHALL record the identifier of the AI_Configuration_Version that produced the Trace, including its prompt, model, output schema, router threshold, retrieval settings, and reranker configuration.
2. IF the AI_Configuration_Version that produced a Trace cannot be resolved when the Tracing_Service records the Trace, THEN THE Tracing_Service SHALL record the Trace with an unresolved AI_Configuration_Version indicator and retain all other recorded Trace data.
3. WHEN an Operator changes an AI_Configuration and provides a change description of 1 to 500 characters, THE Backend_API SHALL create a new AI_Configuration_Version and store the provided change description with it.
4. IF an Operator changes an AI_Configuration with a change description that is empty or exceeds 500 characters, THEN THE Backend_API SHALL reject the change, create no new AI_Configuration_Version, leave the active AI_Configuration_Version unchanged, and return an error indicating the change description is required and must be 1 to 500 characters.
5. WHEN an Operator requests AI_Configuration history, THE Backend_API SHALL return the AI_Configuration_Versions with their change descriptions in reverse chronological order.
6. WHERE no AI_Configuration_Version exists for a requested AI_Configuration history, THE Backend_API SHALL return an empty history collection.
7. THE Backend_API SHALL treat every AI_Configuration_Version as immutable, making no modification to an AI_Configuration_Version after it is created.
8. WHEN an Operator rolls back to an existing previous AI_Configuration_Version, THE Backend_API SHALL set the selected AI_Configuration_Version as the active configuration and SHALL record an Activation_Event capturing the acting Operator, the previous AI_Configuration_Version, the selected AI_Configuration_Version, the timestamp, and the reason.
9. IF an Operator requests a rollback to an AI_Configuration_Version that does not exist, THEN THE Backend_API SHALL leave the active AI_Configuration_Version unchanged and return an error indicating the requested AI_Configuration_Version was not found.
10. WHEN the Backend_API rolls back an AI_Configuration, THE Backend_API SHALL retain all prior AI_Configuration_Versions.
11. WHEN the Tracing_Service records an AI_Configuration_Version in a Trace, THE Tracing_Service SHALL redact sensitive configuration values so that secrets do not appear in the Trace.

### Requirement 10: AI Trace Investigator (Priority 9)

**User Story:** As an Operator, I want an AI assistant in Observability that diagnoses unsuccessful queries and recommends changes, so that I can fix problems without unsafe automatic changes.

#### Acceptance Criteria

1. WHEN an Operator requests a diagnosis of a recorded Trace, THE Trace_Investigator SHALL analyze the recorded route, retrieval scores, rerank order, and generation outcome of the Trace.
2. IF an Operator requests a diagnosis of a Trace that is not recorded, THEN THE Trace_Investigator SHALL perform no diagnosis and return an error indicating the Trace was not found.
3. WHEN the Trace_Investigator completes a diagnosis, THE Trace_Investigator SHALL return a description of the identified cause that references at least one of the analyzed elements: route, retrieval scores, rerank order, or generation outcome.
4. IF the Trace_Investigator cannot determine a cause for the unsuccessful query, THEN THE Trace_Investigator SHALL return a description indicating that no cause was determined and SHALL return zero recommended changes.
5. WHEN the Trace_Investigator completes a diagnosis with an identified cause, THE Trace_Investigator SHALL return between 1 and 10 recommended changes, where each recommended change references the AI_Configuration or the Corpus.
6. THE Trace_Investigator SHALL present recommended changes as recommendations only.
7. THE Trace_Investigator SHALL apply no change to the AI_Configuration or Corpus without explicit Operator action.

### Requirement 11: Knowledge Gap Map (Priority 10)

**User Story:** As an Operator, I want low-quality query outcomes clustered into topics with recommendations, so that I can see which subjects are poorly covered and improve the Corpus.

#### Acceptance Criteria

1. WHEN the Knowledge_Gap_Map is generated, THE Backend_API SHALL cluster into topics the questions that are low-confidence (confidence below the configured confidence threshold), unanswered (resulted in an abstention response), or negatively rated (associated with a Feedback_Item having a negative rating), producing no more than the configured maximum number of topics.
2. WHEN the Knowledge_Gap_Map is generated, THE Backend_API SHALL assign each topic a coverage-quality level and a count of contributing questions.
3. WHEN the Frontend_App displays the Knowledge_Gap_Map, THE Frontend_App SHALL display each topic with its coverage-quality level and its count of contributing questions.
4. WHEN the Knowledge_Gap_Map is generated, THE Backend_API SHALL recommend missing topics or source types that may improve coverage, documents needing re-ingestion, suggested golden Benchmark_Cases, and frequently requested topics.
5. IF generation of the Knowledge_Gap_Map does not complete successfully, THEN THE Backend_API SHALL return an error indicating that the Knowledge_Gap_Map could not be generated.
6. WHERE the total number of eligible query outcomes (low-confidence outcomes, unanswered outcomes, and negatively rated outcomes) is fewer than the configured minimum, THE Frontend_App SHALL display a notice that states the configured minimum and indicates that the Knowledge_Gap_Map requires more accumulated query outcomes.

## Testing Considerations

Per the project's testing preference, new behavior introduced by this feature SHALL be covered by automated tests that match the existing frameworks and patterns in each affected package.

- **Backend (`src/rag_system`, tests under `tests/`)**: Use pytest for behavior and edge cases, and Hypothesis for properties. Property-based tests are appropriate for input-varying logic such as Answer-to-Claim decomposition and Evidence_Status assignment across all four values (`supported`, `partially_supported`, `unsupported`, `verification_unavailable`) given varying Verification_Results (Requirement 1), reason_code coverage for each abstention trigger (Requirement 3), cursor-based pagination boundaries for the Corpus listing and feedback inbox (for example that concatenating successive pages yields each item exactly once and that the final page returns a null next cursor) (Requirements 4 and 6), Corpus_Snapshot immutability round-trips (Requirement 8, for example that serializing and deserializing a Corpus_Snapshot preserves its manifest and that a created snapshot cannot be mutated), Failure_Category storage round-trips (Requirement 6), deterministic evaluation checks and retrieval-metric computation gated on Relevance_Labels (Requirement 7), and Document_Version restore invariants (Requirement 5, for example that restoring a version preserves all prior versions and that at most one Active_Version exists per Document). Round-trip properties SHALL be included for any serialization or parsing of Traces, AI_Configuration_Versions, Corpus_Snapshots, and evaluation results.
- **Frontend (`frontendkimchi`)**: Use Vitest + Testing Library for component and hook behavior and MSW to mock Backend_API responses. Cover primary behaviors and notable edge/empty/error states, such as rendering the four Evidence_Status indicators including `partially_supported` and `verification_unavailable` (Requirement 1), the clarification flow with expired or invalid clarification_id handling and the post-clarification abstention path (Requirement 2), the abstention flow including reason_code display (Requirement 3), paginated Corpus listing including next-cursor navigation and empty state (Requirement 4), the feedback inbox with Review_Status filtering, resolved-item visibility, and full-context display (Requirement 6), the asynchronous Replay_Run states and side-by-side comparison view (Requirement 8), and the insufficient-outcomes notice for the Knowledge_Gap_Map (Requirement 11).
- **Non-property cases**: Infrastructure wiring, external service behavior, and configuration setup SHALL use integration or smoke tests with a small number of representative examples rather than property-based tests.
- The relevant test suite SHALL be run and pass before any implementation task is reported complete.
