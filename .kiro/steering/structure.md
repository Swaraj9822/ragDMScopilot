# Project Structure

## Layout

```
.
├── main.py                  # Root shim: adds src/ to path, exposes `app` for uvicorn main:app
├── rdscon.py                # Standalone RDS/DB connection helper script
├── pyproject.toml           # Build, deps, ruff & pytest config
├── requirements.txt         # Pinned full environment (frozen) — pyproject is the source of truth for deps
├── Dockerfile               # Python 3.11-slim image, non-root user, BM25 baked in
├── docker-compose.yml       # Local full stack: api + worker
├── config/
│   └── copilot_schema_catalog.json   # DB schema catalog the Copilot is allowed to query
├── .github/workflows/deploy.yml      # CI/CD → ECR + ECS Fargate
├── src/rag_system/          # Application package (src-layout)
└── tests/                   # pytest suite (mirrors module names: test_<module>.py)
```

## Application package: `src/rag_system/`

The package is organized by pipeline stage and responsibility. Keep new code aligned to the right module rather than adding cross-cutting logic to `api.py` or `service.py`.

- **`api.py`** — FastAPI app, middleware (logging, trace IDs, per-request timeouts), and HTTP endpoints. Thin layer; delegates to services. Wires dependencies via `@lru_cache` factories (`get_service`, `get_copilot_service`, `get_router`).
- **`config.py`** — `Settings` (pydantic-settings) and `get_settings()`. All configuration lives here.
- **`models.py`** — Pydantic request/response and domain models (`DocumentRecord`, `Chunk`, `QueryRequest/Response`, `UnifiedQuery*`, etc.). Shared data contracts.
- **`service.py`** — `RagService`: orchestrates the ingestion and query pipelines. Lazily constructs pipeline components behind threadsafe properties.
- **`router.py`** — `AgenticRouter` + `BedrockQueryClassifier`: routes `/ask` queries to RAG, database, or hybrid and synthesizes hybrid answers.
- **`copilot.py`** — `DatabaseCopilotService`: NL→SQL over PostgreSQL with `sqlglot`-based validation (`SqlValidationError`).
- **`worker.py`** — `IngestionWorker` background process: polls SQS, processes jobs, extends message visibility, handles graceful shutdown and health file.
- **`queue.py`** — SQS ingestion queue abstraction and job models (`IngestionJob`, `SqsIngestionQueue`).
- **`storage.py`** — `S3ArtifactStore` and S3 key helpers (`document_record_key`, `chunks_key`, `parsed_key`, `embedding_manifest_key`).
- **`parsing.py`** — `DocumentParserRouter`, `SUPPORTED_EXTENSIONS` (LlamaParse-based parsing).
- **`chunking.py`** — `SemanticChunker`.
- **`embedding.py`** — `BedrockTitanEmbedder` (dense embeddings).
- **`sparse.py`** — `BM25SparseEncoder` (sparse vectors).
- **`retrieval.py`** — `PineconeHybridIndex` (upsert, search, delete by document).
- **`generation.py`** — `BedrockNemotronGenerator` (grounded answer generation with citations).
- **`observability.py`** — Logging, metrics, tracing, retry, and timeout helpers (`get_logger`, `metrics`, `timed`, `set_trace_id`/`get_trace_id`, `retry_on_transient`, `RequestTimeoutError`, `setup_logging`).

## Conventions

- **Imports**: use absolute imports rooted at `rag_system` (e.g. `from rag_system.config import Settings`).
- **Dependency injection**: components take `Settings` in their constructor; the API composes them via cached factories. Pass `Settings`/services in rather than calling `get_settings()` deep in the stack.
- **Pipeline flow**: ingestion = parse → chunk → embed (+ sparse) → upsert, with `DocumentStatus` updated and persisted at each step. Query = embed → (sparse) → retrieve → generate.
- **Status & artifacts**: document state is tracked via `DocumentStatus` records persisted in S3; intermediate artifacts (parsed, chunks, manifest) use the key helpers in `storage.py`.
- **Tests** live in `tests/` and mirror module names (`test_router.py`, `test_storage.py`, ...). Add tests alongside new modules using the same naming.
