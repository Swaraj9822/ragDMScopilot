# Product

Production RAG is a Retrieval-Augmented Generation service for answering questions over enterprise data. It combines document-based retrieval with a database Copilot behind a single FastAPI application.

## Core capabilities

- **Document RAG**: Upload business documents (PDF, Office, etc.), which are parsed, semantically chunked, embedded, and indexed for hybrid (dense + BM25 sparse) retrieval. Answers are grounded with citations.
- **Database Copilot**: Answers questions over a PostgreSQL database by generating and safely executing read-only SQL against a known schema catalog.
- **Agentic routing**: A unified `/ask` endpoint uses an LLM classifier to route each question to RAG, the database Copilot, or a hybrid of both, then synthesizes a single answer.
- **Asynchronous ingestion**: Uploads are queued to SQS and processed by a background worker, so long-running parsing/embedding never blocks the API.

## Key qualities

- **Grounded answers**: Responses carry an `evidence_status` (e.g. `grounded`, `partially_grounded`, `insufficient_evidence`) and citations. Do not invent facts beyond retrieved context.
- **Production-ready**: Per-request timeouts, structured logging with trace IDs, CloudWatch metrics, Prometheus `/metrics`, health checks, graceful worker shutdown, and secrets loaded from AWS Secrets Manager.
- **Security-conscious**: Prompt-injection warnings in classifier prompts, SQL validation/read-only enforcement, upload size and file-type limits, non-root container.
