import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

import boto3
import boto3.session
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it works from any working directory
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_prefix="", extra="ignore")

    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(
        default=None, alias="AWS_SECRET_ACCESS_KEY", repr=False
    )
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    s3_bucket: str = Field(alias="RAG_S3_BUCKET")
    s3_kms_key_id: str | None = Field(default=None, alias="RAG_S3_KMS_KEY_ID")
    ingestion_queue_url: str = Field(alias="RAG_INGESTION_QUEUE_URL")
    ingestion_poll_seconds: int = Field(default=20, alias="RAG_INGESTION_POLL_SECONDS")
    ingestion_max_messages: int = Field(default=1, alias="RAG_INGESTION_MAX_MESSAGES")

    llama_cloud_api_key: str = Field(alias="LLAMA_CLOUD_API_KEY", repr=False)

    pinecone_api_key: str = Field(alias="PINECONE_API_KEY", repr=False)
    pinecone_index_name: str = Field(alias="PINECONE_INDEX_NAME")

    cors_allowed_origins: str = Field(default="http://localhost:3000", alias="CORS_ALLOWED_ORIGINS")
    secrets_manager_secret_id: str = Field(default="", alias="SECRETS_MANAGER_SECRET_ID")

    bedrock_embedding_model_id: str = Field(
        default="amazon.titan-embed-text-v2:0", alias="BEDROCK_EMBEDDING_MODEL_ID"
    )
    embedding_dimension: int = Field(default=1024, alias="EMBEDDING_DIMENSION")
    bedrock_model_id: str = Field(default="nvidia.nemotron-super-3-120b", alias="BEDROCK_MODEL_ID")

    # -- Generation provider selection -----------------------------------------
    llm_provider: Literal["bedrock", "gemini"] = Field(default="bedrock", alias="LLM_PROVIDER")
    gemini_model_id: str = Field(default="gemini-1.5-pro", alias="GEMINI_MODEL_ID")
    gcp_project_id: str | None = Field(default=None, alias="GCP_PROJECT_ID")
    gcp_location: str = Field(default="us-central1", alias="GCP_LOCATION")
    google_application_credentials: str | None = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS", repr=False
    )
    llm_fallback_to_bedrock: bool = Field(default=False, alias="LLM_FALLBACK_TO_BEDROCK")
    gemini_read_timeout_s: int = Field(default=90, alias="GEMINI_READ_TIMEOUT_S")

    chunk_target_tokens: int = Field(default=700, alias="RAG_CHUNK_TARGET_TOKENS")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, alias="RAG_MAX_UPLOAD_BYTES")
    retrieval_dense_top_k: int = Field(default=60, alias="RAG_DENSE_TOP_K")
    retrieval_sparse_top_k: int = Field(default=60, alias="RAG_SPARSE_TOP_K")
    sparse_enabled: bool = Field(default=True, alias="RAG_SPARSE_ENABLED")
    low_top_score_threshold: float | None = Field(default=None, alias="RAG_LOW_TOP_SCORE_THRESHOLD")

    copilot_schema_catalog_path: str = Field(
        default="config/copilot_schema_catalog.json",
        alias="COPILOT_SCHEMA_CATALOG_PATH",
    )
    copilot_db_host: str | None = Field(default=None, alias="COPILOT_DB_HOST")
    copilot_db_port: int = Field(default=5432, alias="COPILOT_DB_PORT")
    copilot_db_name: str | None = Field(default=None, alias="COPILOT_DB_NAME")
    copilot_db_user: str | None = Field(default=None, alias="COPILOT_DB_USER")
    copilot_db_password: str | None = Field(default=None, alias="COPILOT_DB_PASSWORD", repr=False)
    copilot_db_sslmode: str = Field(default="require", alias="COPILOT_DB_SSLMODE")
    copilot_max_rows: int = Field(default=100, alias="COPILOT_MAX_ROWS")
    copilot_statement_timeout_ms: int = Field(
        default=10_000,
        alias="COPILOT_STATEMENT_TIMEOUT_MS",
    )

    # -- Catalog loading (S3 fallback) ----------------------------------------
    copilot_schema_catalog_s3_uri: str | None = Field(
        default=None,
        alias="COPILOT_SCHEMA_CATALOG_S3_URI",
    )

    # -- Queue / ingestion resilience -----------------------------------------
    ingestion_max_receive_count: int = Field(
        default=5,
        alias="RAG_INGESTION_MAX_RECEIVE_COUNT",
    )

    # -- Readiness probe -------------------------------------------------------
    readiness_probe_timeout_s: int = Field(
        default=3,
        alias="RAG_READINESS_PROBE_TIMEOUT_S",
    )

    # -- Circuit breaker -------------------------------------------------------
    circuit_failure_threshold: int = Field(
        default=5,
        alias="RAG_CIRCUIT_FAILURE_THRESHOLD",
    )
    circuit_recovery_timeout_s: int = Field(
        default=30,
        alias="RAG_CIRCUIT_RECOVERY_TIMEOUT_S",
    )

    # -- Generation / cost control ---------------------------------------------
    generation_max_context_chars: int = Field(
        default=24000,
        alias="RAG_GENERATION_MAX_CONTEXT_CHARS",
    )
    generation_max_tokens: int = Field(
        default=4096,
        alias="RAG_GENERATION_MAX_TOKENS",
    )
    copilot_sql_max_attempts: int = Field(
        default=3,
        alias="COPILOT_SQL_MAX_ATTEMPTS",
    )

    # -- Pinecone / embedding tuning -------------------------------------------
    pinecone_upsert_batch_size: int = Field(
        default=100,
        alias="RAG_PINECONE_UPSERT_BATCH_SIZE",
    )
    embedding_max_workers: int = Field(
        default=10,
        alias="RAG_EMBEDDING_MAX_WORKERS",
    )

    # -- Request-level timeouts (seconds) ----------------------------------
    # Maximum wall-clock time allowed for each endpoint class before the
    # server returns HTTP 504.  Set to 0 to disable (not recommended).
    request_timeout_query_s: int = Field(
        default=60,
        alias="RAG_REQUEST_TIMEOUT_QUERY_S",
    )
    request_timeout_copilot_s: int = Field(
        default=90,
        alias="RAG_REQUEST_TIMEOUT_COPILOT_S",
    )
    request_timeout_ask_s: int = Field(
        default=240,
        alias="RAG_REQUEST_TIMEOUT_ASK_S",
    )

    # Bedrock SDK read timeout per individual LLM / embedding call (seconds).
    # This caps each *single* Bedrock Converse or InvokeModel call.
    bedrock_read_timeout_s: int = Field(
        default=90,
        alias="BEDROCK_READ_TIMEOUT_S",
    )

    # -- Reranker ------------------------------------------------------------------
    reranker_enabled: bool = Field(default=False, alias="RAG_RERANKER_ENABLED")
    reranker_model_id: str = Field(
        default="gemini-3.5-flash", alias="RAG_RERANKER_MODEL_ID", max_length=128
    )
    reranker_top_k: int = Field(default=10, alias="RAG_RERANKER_TOP_K", ge=1, le=100)
    reranker_score_threshold: float | None = Field(
        default=None, alias="RAG_RERANKER_SCORE_THRESHOLD", ge=0.0, le=1.0
    )
    reranker_max_concurrent: int = Field(
        default=5, alias="RAG_RERANKER_MAX_CONCURRENT", ge=1, le=50
    )
    reranker_timeout_s: int = Field(default=30, alias="RAG_RERANKER_TIMEOUT_S", ge=1, le=300)

    def model_post_init(self, __context) -> None:
        if self.secrets_manager_secret_id:
            try:
                client = self.boto3_session().client("secretsmanager")
                response = client.get_secret_value(SecretId=self.secrets_manager_secret_id)
                secrets = json.loads(response["SecretString"])
                for k, v in secrets.items():
                    for field_name, field in self.model_fields.items():
                        if field.alias == k or field_name == k:
                            setattr(self, field_name, v)
            except Exception as e:
                raise RuntimeError(f"Failed to load secrets from AWS Secrets Manager: {e}")

        if not self.s3_bucket:
            raise ValueError("s3_bucket must be set")
        if not self.ingestion_queue_url.startswith("https://sqs."):
            raise ValueError(f"Invalid SQS queue URL: {self.ingestion_queue_url}")

    def boto3_session(self) -> boto3.session.Session:
        """Return a boto3 Session pre-loaded with credentials from .env.

        Falls back to the default credential chain (IAM role, ~/.aws/credentials)
        when the keys are not set — safe for production deployments.
        """
        return boto3.session.Session(
            aws_access_key_id=self.aws_access_key_id or None,
            aws_secret_access_key=self.aws_secret_access_key or None,
            region_name=self.aws_region,
        )

    def bedrock_botocore_config(self):
        """Return a botocore Config that caps individual Bedrock call read time.

        This prevents a single Bedrock Converse / InvokeModel call from
        hanging indefinitely if the upstream model is slow or stuck.
        """
        from botocore.config import Config as BotoConfig

        return BotoConfig(
            read_timeout=self.bedrock_read_timeout_s,
            retries={"max_attempts": 0},  # retries are handled by tenacity
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
