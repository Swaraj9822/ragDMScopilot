# Production RAG

This is a production-ready Retrieval-Augmented Generation (RAG) system built with FastAPI, Pinecone, LlamaParse, AWS Bedrock (Titan/Nemotron), and AWS S3/SQS. It features a robust multi-agent architecture with a database Copilot, gracefully handled long-running background ingestion workers, and comprehensive telemetry (CloudWatch metrics, structured logging).

## Prerequisites
- **Python 3.11+**
- **Docker & Docker Compose** (optional, but recommended for easy local execution)
- **AWS Account** with configured credentials (IAM user/role with permissions for S3, SQS, Bedrock, and Secrets Manager).
- **Pinecone** API Key and an active Index.
- **LlamaCloud** API Key.
- A **PostgreSQL** database (only if you want to test the Database Copilot features).

## Initial Setup

1. **Environment Variables**: 
   Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
   Open `.env` and fill in your keys. 
   - Ensure your `AWS_REGION`, `RAG_S3_BUCKET`, and `RAG_INGESTION_QUEUE_URL` are correct.
   - Set `SECRETS_MANAGER_SECRET_ID` to empty (`""`) if you want to test locally by explicitly setting keys in the `.env` file instead of fetching from AWS Secrets Manager.

2. **AWS Bedrock Access**:
   Make sure you have navigated to the Amazon Bedrock console in your specified AWS Region and requested access to your chosen embeddings model (`amazon.titan-embed-text-v2:0`) and generation model (`nvidia.nemotron-super-3-120b`).

## Running Locally (Docker Compose)

The easiest way to run the full stack (API + Worker) locally is via Docker Compose:

```bash
docker-compose up --build
```

This will:
- Build the `production-rag` Docker image.
- Spin up the web API container on `http://localhost:8000`.
- Spin up the background ingestion worker container to process SQS tasks.

## Running Locally (Native Python / Dev Mode)

If you prefer to run the system natively for rapid development:

1. **Install Dependencies**:
   ```bash
   python -m pip install -e .[dev]
   ```

2. **Start the Web API**:
   ```bash
   uvicorn rag_system.api:app --reload --host 0.0.0.0 --port 8000
   ```
   The interactive API docs will be available at `http://localhost:8000/docs`.

3. **Start the Background Worker** (in a separate terminal window):
   ```bash
   python -m rag_system.worker
   ```

## Testing

To run the automated test suite, ensure you have installed the `[dev]` dependencies:
```bash
pytest tests/
```

This will run all unit tests, integration tests, and endpoint smoke tests.
