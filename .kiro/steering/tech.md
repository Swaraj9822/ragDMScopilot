# Tech Stack

## Language & runtime
- **Python 3.11+** (uses 3.11 features like `StrEnum`, `X | None` unions).
- Package built with **Hatchling**; source lives under `src/` (src-layout). Configured in `pyproject.toml`.

## Core libraries
- **FastAPI** + **Uvicorn** — web API and ASGI server.
- **Pydantic v2** + **pydantic-settings** — data models and environment-driven config.
- **boto3 / botocore** — AWS access (S3, SQS, Bedrock, Secrets Manager, CloudWatch).
- **LlamaParse / llama-cloud-services / llama-index-core** — document parsing and indexing primitives.
- **Pinecone** + **pinecone-text** (BM25) — hybrid dense/sparse vector search.
- **AWS Bedrock** — embeddings (`amazon.titan-embed-text-v2:0`) and generation (`nvidia.nemotron-super-3-120b`).
- **psycopg (v3)** + **psycopg-pool** — PostgreSQL access for the Copilot.
- **sqlglot** — SQL parsing/validation for safe read-only query enforcement.
- **tenacity** — retry on transient failures (see `retry_on_transient` / `@retry_on_transient()`).

## Configuration
- All settings flow through `rag_system.config.Settings` (a `BaseSettings`), accessed via the cached `get_settings()`.
- Settings are read from environment variables / `.env` using explicit `alias` names (e.g. `RAG_S3_BUCKET`, `PINECONE_API_KEY`). Add new config as `Field(..., alias="ENV_NAME")`, never read `os.environ` directly.
- Secrets can be loaded from AWS Secrets Manager when `SECRETS_MANAGER_SECRET_ID` is set; otherwise from `.env`.
- Mark sensitive fields with `repr=False`.

## Tooling
- **Ruff** for lint/format — `line-length = 100`, `target-version = "py311"`.
- **pytest** + **pytest-asyncio** for tests; `pythonpath = ["src"]`, `testpaths = ["tests"]`.

## Common commands

```bash
# Install (with dev tools)
python -m pip install -e .[dev]

# Run the API locally (interactive docs at /docs)
uvicorn rag_system.api:app --reload --host 0.0.0.0 --port 8000
# or via the root shim:
uvicorn main:app --reload

# Run the background ingestion worker (separate terminal)
python -m rag_system.worker

# Run the full stack (API + worker) in Docker
docker-compose up --build

# Lint / format
ruff check .
ruff format .

# Tests
pytest tests/
```

## Deployment
- Containerized via `Dockerfile` (Python 3.11-slim, non-root `appuser`, BM25 model baked in at build time).
- CI/CD in `.github/workflows/deploy.yml`: on push to `main`, builds and pushes to **ECR**, then deploys to **AWS ECS Fargate**.

## Conventions
- Prefer async endpoints; offload blocking/CPU-bound work with `run_in_threadpool`.
- Use the observability helpers from `rag_system.observability`: `get_logger(__name__)`, `metrics.increment/observe`, `timed(...)` context manager, and trace-id helpers (`set_trace_id` / `get_trace_id`).
- Apply `@retry_on_transient()` to network/LLM calls rather than hand-rolling retries.
- Cap external calls with timeouts (per-request timeouts in middleware; Bedrock read timeout via `settings.bedrock_botocore_config()`).
