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
    ingestion_max_messages: int = Field(default=10, alias="RAG_INGESTION_MAX_MESSAGES")
    # How many received ingestion messages to process concurrently within a
    # single poll cycle. Bounded so a burst upload drains in parallel instead of
    # one-at-a-time, without swamping Bedrock/Pinecone. Ensure the SQS queue's
    # visibility timeout comfortably exceeds worst-case ingestion time so an
    # in-flight message is not redelivered while still being processed.
    ingestion_max_concurrency: int = Field(
        default=4, alias="RAG_INGESTION_MAX_CONCURRENCY"
    )

    llama_cloud_api_key: str = Field(alias="LLAMA_CLOUD_API_KEY")

    pinecone_api_key: str = Field(alias="PINECONE_API_KEY")
    pinecone_index_name: str = Field(alias="PINECONE_INDEX_NAME")

    bedrock_embedding_model_id: str = Field(
        default="amazon.titan-embed-text-v2:0", alias="BEDROCK_EMBEDDING_MODEL_ID"
    )
    embedding_dimension: int = Field(default=1024, alias="EMBEDDING_DIMENSION")
    # Titan v2 has no batch embedding API, so chunks are embedded one request
    # each. Issuing those requests concurrently (bounded) turns a serial
    # per-chunk round-trip into a parallel fan-out — the dominant ingestion
    # latency win for multi-hundred-chunk documents.
    embedding_max_workers: int = Field(default=8, alias="RAG_EMBEDDING_MAX_WORKERS")
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
    # Pinecone recommends batches of ~100 vectors per upsert; a large document
    # can otherwise exceed the request size limit and fail the whole ingestion.
    pinecone_upsert_batch_size: int = Field(
        default=100, alias="RAG_PINECONE_UPSERT_BATCH_SIZE"
    )
    # Concurrency for fanning out the per-document S3 reads behind
    # ``GET /documents`` so the listing does not degrade to N serial round-trips.
    document_list_max_workers: int = Field(
        default=16, alias="RAG_DOCUMENT_LIST_MAX_WORKERS"
    )
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
    # Defense-in-depth: point COPILOT_DB_USER at a dedicated role that holds only
    # SELECT grants on the approved tables and has `default_transaction_read_only
    # = on`. Then even if a crafted query slips past CopilotSqlGuard, the database
    # itself refuses writes — validation is no longer the last line of defense.
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

    @field_validator(
        "embedding_max_workers",
        "pinecone_upsert_batch_size",
        "document_list_max_workers",
        "ingestion_max_concurrency",
    )
    @classmethod
    def _validate_positive_bounded_int(cls, value: int) -> int:
        """These concurrency/batch knobs must be a sane positive integer.

        A zero/negative value would stall or crash the corresponding fan-out,
        and an absurdly large one would defeat the bound it exists to enforce,
        so we clamp the accepted range to ``[1, 1000]`` and fail fast otherwise.
        """
        if value < 1 or value > 1000:
            raise ValueError(
                f"invalid value {value!r}: must be within 1 and 1000 inclusive"
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

    # --- Authentication (self-managed JWT) ---
    # When auth is enabled every endpoint except the public ones (/, /health,
    # /metrics, /docs, and /auth/*) requires a valid bearer token. Disable it
    # (RAG_AUTH_ENABLED=false) only for trusted local/single-user setups.
    auth_enabled: bool = Field(default=True, alias="RAG_AUTH_ENABLED")
    # Optional dedicated bearer token for the /metrics scrape endpoint. When set,
    # /metrics requires "Authorization: Bearer <token>" (configure your
    # Prometheus scrape job's bearer_token accordingly), closing the default
    # public exposure that leaks route latencies, error rates, and model ids.
    # When unset the endpoint stays open for backward compatibility — set this in
    # production, or block /metrics at the reverse proxy and keep only /health open.
    metrics_token: str | None = Field(default=None, alias="RAG_METRICS_TOKEN")
    # Open self-service registration. When False (default), /auth/register is
    # closed once at least one account exists — the first registration is still
    # allowed so an initial operator can bootstrap, then the endpoint locks down
    # and further accounts must be provisioned deliberately (flip this to True).
    # This keeps an internal single-user tool from accepting arbitrary signups.
    auth_allow_registration: bool = Field(
        default=False, alias="RAG_AUTH_ALLOW_REGISTRATION"
    )
    # Per-client rate limiting for abuse-prone auth endpoints (login, register,
    # refresh). In-process/per-replica only; front with a shared limiter for
    # multi-instance deployments. Set the per-minute allowance to 0 to disable.
    auth_rate_limit_per_minute: int = Field(
        default=10, alias="RAG_AUTH_RATE_LIMIT_PER_MINUTE"
    )

    @field_validator("auth_rate_limit_per_minute")
    @classmethod
    def _validate_auth_rate_limit(cls, value: int) -> int:
        if value < 0 or value > 10_000:
            raise ValueError(
                f"invalid auth rate limit {value!r} per minute: must be within "
                "0 (disabled) and 10000 inclusive"
            )
        return value
    # Comma-separated list of browser origins allowed to make credentialed
    # cross-origin requests. A wildcard is intentionally NOT the default:
    # browsers reject "*" together with credentials, and an open wildcard lets
    # any site call the API with the user's cookies/authorization. Defaults to
    # the local RAG Console dev origin; set RAG_CORS_ALLOW_ORIGINS to the real
    # console URL(s) in production.
    cors_allow_origins: str = Field(
        default="http://localhost:3000", alias="RAG_CORS_ALLOW_ORIGINS"
    )

    @property
    def cors_allow_origins_list(self) -> list[str]:
        """Parsed, de-duplicated list of allowed CORS origins."""
        seen: list[str] = []
        for origin in self.cors_allow_origins.split(","):
            origin = origin.strip().rstrip("/")
            if origin and origin not in seen:
                seen.append(origin)
        return seen
    # HMAC signing secret for HS256 tokens. Required when auth is enabled; the
    # application refuses to start without it so tokens are never signed with a
    # predictable key. Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret_key: str | None = Field(default=None, alias="RAG_JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="RAG_JWT_ALGORITHM")
    jwt_issuer: str = Field(default="production-rag", alias="RAG_JWT_ISSUER")
    access_token_ttl_minutes: int = Field(
        default=60, alias="RAG_ACCESS_TOKEN_TTL_MINUTES"
    )
    refresh_token_ttl_days: int = Field(
        default=30, alias="RAG_REFRESH_TOKEN_TTL_DAYS"
    )

    @field_validator("access_token_ttl_minutes")
    @classmethod
    def _validate_access_token_ttl(cls, value: int) -> int:
        if value < 1 or value > 7 * 24 * 60:  # 1 minute .. 7 days
            raise ValueError(
                f"invalid access token TTL {value!r} minutes: must be within "
                "1 minute and 7 days (10080 minutes) inclusive"
            )
        return value

    @field_validator("refresh_token_ttl_days")
    @classmethod
    def _validate_refresh_token_ttl(cls, value: int) -> int:
        if value < 1 or value > 365:  # 1 day .. 1 year
            raise ValueError(
                f"invalid refresh token TTL {value!r} days: must be within "
                "1 day and 365 days inclusive"
            )
        return value

    # --- Refresh-token cookie ---
    # The refresh token is delivered to browsers as an httpOnly cookie (never in
    # the JSON body) so page JavaScript — and thus an XSS payload — cannot read
    # it. These knobs tune the cookie for the deployment's origin topology:
    #   * Same-origin console + API: SameSite=Lax, Secure=True is ideal.
    #   * Cross-origin console (different host/port, the dev default): the cookie
    #     must be SameSite=None; Secure so the browser sends it on the
    #     credentialed cross-site refresh call. Browsers treat localhost as a
    #     secure context, so None+Secure also works over http://localhost in dev.
    #   * Plain-http, non-localhost dev: set RAG_AUTH_COOKIE_SECURE=false (and
    #     SameSite=Lax) so the browser will store the cookie.
    auth_cookie_secure: bool = Field(default=True, alias="RAG_AUTH_COOKIE_SECURE")
    auth_cookie_samesite: str = Field(default="none", alias="RAG_AUTH_COOKIE_SAMESITE")
    auth_cookie_domain: str | None = Field(default=None, alias="RAG_AUTH_COOKIE_DOMAIN")

    @field_validator("auth_cookie_samesite")
    @classmethod
    def _validate_cookie_samesite(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError(
                f"invalid auth cookie SameSite {value!r}: must be one of "
                "'lax', 'strict', or 'none'"
            )
        return normalized

    def require_jwt_secret(self) -> str:
        """Return the configured JWT secret or raise when auth is misconfigured.

        Centralises the "auth enabled but no secret set" failure so it surfaces
        with a clear, actionable message wherever a token is signed or verified.
        """
        if not self.jwt_secret_key:
            raise RuntimeError(
                "Authentication is enabled but RAG_JWT_SECRET_KEY is not set. "
                "Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self.jwt_secret_key

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
