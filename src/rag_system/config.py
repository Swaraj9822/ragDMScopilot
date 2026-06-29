from functools import lru_cache
from pathlib import Path
from typing import Literal

import boto3
import boto3.session
from botocore.config import Config as BotoConfig
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it works from any working directory
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_prefix="", extra="ignore")

    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    s3_bucket: str = Field(alias="RAG_S3_BUCKET")
    s3_kms_key_id: str | None = Field(default=None, alias="RAG_S3_KMS_KEY_ID")
    ingestion_queue_url: str = Field(alias="RAG_INGESTION_QUEUE_URL")
    ingestion_poll_seconds: int = Field(default=20, alias="RAG_INGESTION_POLL_SECONDS")
    ingestion_max_messages: int = Field(default=1, alias="RAG_INGESTION_MAX_MESSAGES")

    llama_cloud_api_key: str = Field(alias="LLAMA_CLOUD_API_KEY")

    pinecone_api_key: str = Field(alias="PINECONE_API_KEY")
    pinecone_index_name: str = Field(alias="PINECONE_INDEX_NAME")

    bedrock_embedding_model_id: str = Field(
        default="amazon.titan-embed-text-v2:0", alias="BEDROCK_EMBEDDING_MODEL_ID"
    )
    embedding_dimension: int = Field(default=1024, alias="EMBEDDING_DIMENSION")
    bedrock_rerank_model_id: str = Field(
        default="cohere.rerank-v3-5:0", alias="BEDROCK_RERANK_MODEL_ID"
    )

    # --- Text-generation LLM (Google Gemini on Vertex AI) ---
    gemini_model_id: str = Field(default="gemini-3.5-flash", alias="GEMINI_MODEL_ID")
    gcp_project_id: str | None = Field(default=None, alias="GCP_PROJECT_ID")
    gcp_location: str = Field(default="us-central1", alias="GCP_LOCATION")
    google_application_credentials: str | None = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    gemini_read_timeout_s: int = Field(default=90, alias="GEMINI_READ_TIMEOUT_S")
    # Thinking-token budget for Gemini reasoning models. None = SDK default
    # (dynamic). 0 disables thinking on models that support it (e.g. Flash).
    gemini_thinking_budget: int | None = Field(
        default=None, alias="GEMINI_THINKING_BUDGET"
    )

    @property
    def active_llm_model_id(self) -> str:
        """Model id of the text-generation provider (Gemini)."""
        return self.gemini_model_id

    chunk_target_tokens: int = Field(default=700, alias="RAG_CHUNK_TARGET_TOKENS")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, alias="RAG_MAX_UPLOAD_BYTES")
    retrieval_dense_top_k: int = Field(default=60, alias="RAG_DENSE_TOP_K")
    retrieval_sparse_top_k: int = Field(default=60, alias="RAG_SPARSE_TOP_K")
    rerank_top_k: int = Field(default=12, alias="RAG_RERANK_TOP_K")
    rerank_enabled: bool = Field(default=False, alias="RAG_RERANK_ENABLED")
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
    copilot_db_password: str | None = Field(default=None, alias="COPILOT_DB_PASSWORD")
    copilot_db_sslmode: str = Field(default="require", alias="COPILOT_DB_SSLMODE")
    copilot_max_rows: int = Field(default=100, alias="COPILOT_MAX_ROWS")
    copilot_statement_timeout_ms: int = Field(
        default=10_000,
        alias="COPILOT_STATEMENT_TIMEOUT_MS",
    )

    # --- Hybrid synthesis ---
    # The hybrid route runs RAG and the database copilot, then optionally makes
    # one more LLM call to merge the two answers (~3s on the critical path).
    # "auto"   — synthesize only when the two answers share enough vocabulary to
    #            actually overlap; otherwise present them as labeled sections.
    # "always" — always make the merge call (previous behaviour).
    # "never"  — never merge; always present labeled sections.
    hybrid_synthesis_mode: Literal["auto", "always", "never"] = Field(
        default="auto", alias="RAG_HYBRID_SYNTHESIS_MODE"
    )
    # Overlap coefficient (shared significant tokens / smaller token set) at or
    # above which "auto" mode treats the two answers as overlapping.
    hybrid_overlap_threshold: float = Field(
        default=0.12, alias="RAG_HYBRID_OVERLAP_THRESHOLD"
    )

    @field_validator("hybrid_overlap_threshold")
    @classmethod
    def _validate_overlap_threshold(cls, value: float) -> float:
        if value < 0.0 or value > 1.0:
            raise ValueError(
                f"invalid hybrid overlap threshold {value!r}: must be within "
                "the inclusive range [0.0, 1.0]"
            )
        return value

    # AWS client tuning. Bedrock/S3 latency from some environments is erratic;
    # explicit timeouts + adaptive retries fail fast and back off on throttling
    # instead of hanging on a single slow call.
    aws_connect_timeout_s: int = Field(default=5, alias="AWS_CONNECT_TIMEOUT_S")
    aws_read_timeout_s: int = Field(default=30, alias="AWS_READ_TIMEOUT_S")
    aws_max_attempts: int = Field(default=4, alias="AWS_MAX_ATTEMPTS")

    # --- Observability / tracing platform ---
    # New tracing settings use the existing alias mechanism (R10.9). The
    # sample-rate and retention bounds are validated at startup (R10.6) so a
    # misconfigured deployment fails fast through the get_settings() path.
    tracing_enabled: bool = Field(default=True, alias="RAG_TRACING_ENABLED")
    trace_sample_rate: float = Field(default=1.0, alias="RAG_TRACE_SAMPLE_RATE")
    trace_retention_hours: int | None = Field(
        default=None, alias="RAG_TRACE_RETENTION_HOURS"
    )
    log_retention_hours: int | None = Field(
        default=None, alias="RAG_LOG_RETENTION_HOURS"
    )
    retention_interval_hours: int = Field(
        default=24, alias="RAG_RETENTION_INTERVAL_HOURS"
    )
    trace_buffer_capacity: int = Field(default=10_000, alias="RAG_TRACE_BUFFER_CAPACITY")
    log_buffer_capacity: int = Field(default=10_000, alias="RAG_LOG_BUFFER_CAPACITY")

    @field_validator("trace_sample_rate")
    @classmethod
    def _validate_trace_sample_rate(cls, value: float) -> float:
        """Reject a non-numeric or out-of-range sampling rate at startup (R10.6).

        Pydantic already rejects values that cannot be coerced to ``float``;
        here we additionally enforce the inclusive ``[0.0, 1.0]`` range and emit
        an error that names the invalid value.
        """
        if value < 0.0 or value > 1.0:
            raise ValueError(
                f"invalid trace sampling rate {value!r}: must be a number "
                "within the inclusive range [0.0, 1.0]"
            )
        return value

    @field_validator("trace_retention_hours", "log_retention_hours")
    @classmethod
    def _validate_retention_hours(cls, value: int | None) -> int | None:
        """Validate retention bounds (1 hour to 3650 days) when present (R10.6).

        A retention period is optional; when configured it must fall within
        1 hour and 3650 days (87,600 hours) inclusive.
        """
        if value is None:
            return value
        _MIN_RETENTION_HOURS = 1
        _MAX_RETENTION_HOURS = 3650 * 24  # 87,600 hours
        if value < _MIN_RETENTION_HOURS or value > _MAX_RETENTION_HOURS:
            raise ValueError(
                f"invalid retention period {value!r} hours: must be within "
                f"{_MIN_RETENTION_HOURS} hour and {_MAX_RETENTION_HOURS} hours "
                "(3650 days) inclusive"
            )
        return value

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

    def boto3_client_config(self) -> BotoConfig:
        """botocore Config with bounded timeouts and adaptive retries."""
        return BotoConfig(
            connect_timeout=self.aws_connect_timeout_s,
            read_timeout=self.aws_read_timeout_s,
            retries={"max_attempts": self.aws_max_attempts, "mode": "adaptive"},
        )

    def bedrock_runtime_client(self):
        """bedrock-runtime client with tuned timeouts/retries."""
        return self.boto3_session().client(
            "bedrock-runtime", config=self.boto3_client_config()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
