from functools import lru_cache
from pathlib import Path
from typing import Literal

import boto3
import boto3.session
from botocore.config import Config as BotoConfig
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it works from any working directory
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class ModelPricing(BaseModel):
    """USD price per 1,000 tokens for a single model id.

    Used to convert per-query prompt/completion token counts into a monetary
    ``cost`` for replay comparison and cost reporting (R11.1, R11.6). Prices are
    non-negative; a zero price is allowed (e.g. a free/self-hosted model).
    """

    prompt_usd_per_1k: float = Field(ge=0.0)
    completion_usd_per_1k: float = Field(ge=0.0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_prefix="", extra="ignore")

    # --- AWS (retained only for the optional, currently-disabled Bedrock
    # reranker). The rest of the data/AI plane now runs on GCP. ---
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    # --- Google Cloud Storage artifact store (was AWS S3) ---
    gcs_bucket: str = Field(alias="RAG_GCS_BUCKET")
    # Optional Cloud KMS key for CMEK (customer-managed) object encryption, as a
    # full resource name:
    # projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>. GCS encrypts at
    # rest by default, so this is only needed when CMEK is required.
    gcs_kms_key_name: str | None = Field(default=None, alias="RAG_GCS_KMS_KEY_NAME")

    # --- Cloud Pub/Sub ingestion queue (was AWS SQS) ---
    # Optional so tests/local setups that never touch the queue can construct
    # Settings without them; the queue client validates their presence at use.
    pubsub_topic_id: str | None = Field(default=None, alias="RAG_PUBSUB_TOPIC_ID")
    pubsub_subscription_id: str | None = Field(
        default=None, alias="RAG_PUBSUB_SUBSCRIPTION_ID"
    )
    ingestion_poll_seconds: int = Field(default=20, alias="RAG_INGESTION_POLL_SECONDS")
    ingestion_max_messages: int = Field(default=10, alias="RAG_INGESTION_MAX_MESSAGES")
    # How many received ingestion messages to process concurrently within a
    # single poll cycle. Bounded so a burst upload drains in parallel instead of
    # one-at-a-time, without swamping the embedder/Pinecone. Ensure the Pub/Sub
    # subscription's ack deadline comfortably exceeds worst-case ingestion time
    # so an in-flight message is not redelivered while still being processed.
    ingestion_max_concurrency: int = Field(
        default=4, alias="RAG_INGESTION_MAX_CONCURRENCY"
    )

    llama_cloud_api_key: str = Field(alias="LLAMA_CLOUD_API_KEY")

    pinecone_api_key: str = Field(alias="PINECONE_API_KEY")
    pinecone_index_name: str = Field(alias="PINECONE_INDEX_NAME")
    # Serverless placement + metric used when (re)creating the index via
    # scripts/create_pinecone_index.py. "dotproduct" is required for Pinecone
    # sparse+dense hybrid search. The cloud/region below are Pinecone-hosted
    # infrastructure and are unrelated to the user's own AWS account.
    pinecone_cloud: str = Field(default="aws", alias="RAG_PINECONE_CLOUD")
    pinecone_region: str = Field(default="us-east-1", alias="RAG_PINECONE_REGION")
    pinecone_metric: str = Field(default="dotproduct", alias="RAG_PINECONE_METRIC")

    # --- Embeddings (Google Gemini on Vertex AI; was AWS Bedrock Titan) ---
    embedding_model_id: str = Field(
        default="gemini-embedding-001", alias="EMBEDDING_MODEL_ID"
    )
    # gemini-embedding-001 emits 3072-dim vectors by default (Matryoshka:
    # 768/1536/3072 are the recommended sizes). The Pinecone index dimension
    # must match this value.
    embedding_dimension: int = Field(default=3072, alias="EMBEDDING_DIMENSION")
    # The Vertex embedding API embeds one text per request, so chunks are
    # embedded one request each. Issuing those requests concurrently (bounded)
    # turns a serial per-chunk round-trip into a parallel fan-out — the dominant
    # ingestion latency win for multi-hundred-chunk documents.
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

    # --- Multi-turn conversations ---
    # When enabled, follow-up questions are rewritten into standalone queries
    # using the stored conversation history before routing/retrieval, and each
    # turn is persisted server-side so the next turn can reference it.
    conversation_rewrite_enabled: bool = Field(
        default=True, alias="RAG_CONVERSATION_REWRITE_ENABLED"
    )
    # Newest turns kept per conversation. Bounds both the rewrite prompt size
    # and the stored record; older turns are dropped once the cap is exceeded.
    conversation_max_turns: int = Field(
        default=12, alias="RAG_CONVERSATION_MAX_TURNS"
    )
    # How many of the most recent turns are fed to the follow-up rewriter. Kept
    # small so the rewrite call stays cheap and focused on recent context.
    conversation_rewrite_window: int = Field(
        default=6, alias="RAG_CONVERSATION_REWRITE_WINDOW"
    )

    @field_validator("conversation_max_turns", "conversation_rewrite_window")
    @classmethod
    def _validate_conversation_bounds(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError(
                f"invalid conversation window {value!r}: must be within 1 and 100 inclusive"
            )
        return value

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
        "ingestion_max_messages",
    )
    @classmethod
    def _validate_positive_bounded_int(cls, value: int) -> int:
        """These concurrency/batch knobs must be a sane positive integer.

        A zero/negative value would stall or crash the corresponding fan-out,
        and an absurdly large one would defeat the bound it exists to enforce,
        so we clamp the accepted range to ``[1, 1000]`` and fail fast otherwise.
        ``ingestion_max_messages`` shares this bound because Pub/Sub accepts at
        most 1000 messages per pull, so anything larger is rejected at startup
        rather than being silently truncated at pull time.
        """
        if value < 1 or value > 1000:
            raise ValueError(
                f"invalid value {value!r}: must be within 1 and 1000 inclusive"
            )
        return value

    # AWS client tuning. Retained for the optional (currently disabled) Bedrock
    # reranker: explicit timeouts + adaptive retries fail fast and back off on
    # throttling instead of hanging on a single slow call.
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

    # --- Answer path / abstention (R2, R3) ---
    # Per-route minimum routing confidence: below this the router treats a
    # classification as too uncertain to answer directly. Retrieval score
    # threshold gates whether retrieved hits are strong enough to ground an
    # answer. Both are confidences in the inclusive range [0.0, 1.0].
    route_min_confidence: float = Field(default=0.5, alias="RAG_ROUTE_MIN_CONFIDENCE")
    retrieval_score_threshold: float = Field(
        default=0.3, alias="RAG_RETRIEVAL_SCORE_THRESHOLD"
    )
    # How long an issued clarification remains valid; a reply after this window
    # is rejected as expired (R2.6).
    clarification_expiry_minutes: int = Field(
        default=30, alias="RAG_CLARIFICATION_EXPIRY_MINUTES"
    )
    # Ceiling on concurrent (claim, evidence) entailment calls during claim
    # verification (R1.3). The per-pair calls are otherwise a serial
    # O(claims x hits) chain on the answer path; a bounded pool parallelizes
    # them without changing the per-pair verdicts or the evidence-item count.
    claim_verification_max_workers: int = Field(
        default=8, alias="RAG_CLAIM_VERIFICATION_MAX_WORKERS"
    )

    # --- Corpus inventory / listing (R4) ---
    # Maximum documents returned per corpus page. Requests above this are
    # clamped down to it; the value itself must fall within [1, 100] (R4.4).
    corpus_page_size: int = Field(default=50, alias="RAG_CORPUS_PAGE_SIZE")
    # HMAC secret used to sign opaque pagination cursors so a tampered or forged
    # cursor is rejected rather than silently trusted (R4.6). Optional in
    # development; set a strong secret in production.
    pagination_signing_key: str | None = Field(
        default=None, alias="RAG_PAGINATION_SIGNING_KEY"
    )

    # --- Feedback review inbox (R6) ---
    # Evaluation_Set that reviewed Feedback_Items are promoted into (R6.6). A
    # single default set is sufficient today; multi-set promotion can extend the
    # endpoint contract later without changing the promotion logic.
    default_evaluation_set_id: str = Field(
        default="default", alias="RAG_DEFAULT_EVALUATION_SET_ID"
    )

    # --- Evaluation / LLM judge (R7) ---
    # Depth k at which retrieval metrics (recall@k, precision@k, MRR@k) are
    # computed against relevance labels.
    retrieval_metric_depth_k: int = Field(
        default=10, alias="RAG_RETRIEVAL_METRIC_DEPTH_K"
    )
    # The LLM judge uses a thinking model distinct from the generation model.
    llm_judge_model_id: str = Field(
        default="gemini-3.1-pro", alias="RAG_LLM_JUDGE_MODEL_ID"
    )
    # Bounded thinking-token budget for the judge model.
    llm_judge_thinking_budget: int = Field(
        default=4096, alias="RAG_LLM_JUDGE_THINKING_BUDGET"
    )
    # Read timeout for a single judge call (~55s) sized to fit inside the fixed
    # per-case timeout (60s) so a slow call is recorded as a per-case error
    # rather than hanging the run (R7.8).
    llm_judge_read_timeout_s: int = Field(
        default=55, alias="RAG_LLM_JUDGE_READ_TIMEOUT_S"
    )
    llm_judge_per_case_timeout_s: int = Field(
        default=60, alias="RAG_LLM_JUDGE_PER_CASE_TIMEOUT_S"
    )
    # How often the (out-of-CI) LLM judge evaluation is scheduled to run.
    llm_judge_schedule_interval_hours: int = Field(
        default=24, alias="RAG_LLM_JUDGE_SCHEDULE_INTERVAL_HOURS"
    )

    # --- Trace investigator (R10) ---
    trace_investigator_model_id: str = Field(
        default="gemini-3.1-pro", alias="RAG_TRACE_INVESTIGATOR_MODEL_ID"
    )
    trace_investigator_thinking_budget: int = Field(
        default=4096, alias="RAG_TRACE_INVESTIGATOR_THINKING_BUDGET"
    )
    trace_investigator_read_timeout_s: int = Field(
        default=55, alias="RAG_TRACE_INVESTIGATOR_READ_TIMEOUT_S"
    )

    # --- Knowledge-gap analysis (R11) ---
    # Maximum topics surfaced in a generated knowledge-gap map, and the minimum
    # number of eligible outcomes required before a topic is reported (R11.6).
    knowledge_gap_max_topics: int = Field(
        default=25, alias="RAG_KNOWLEDGE_GAP_MAX_TOPICS"
    )
    knowledge_gap_min_eligible_outcomes: int = Field(
        default=20, alias="RAG_KNOWLEDGE_GAP_MIN_ELIGIBLE_OUTCOMES"
    )

    # --- Replay and compare lab (R8, R11) ---
    # Wall-clock budget for a single replay job before it is timed out.
    replay_job_timeout_s: int = Field(default=300, alias="RAG_REPLAY_JOB_TIMEOUT_S")
    # Per-model USD pricing per 1,000 tokens, keyed by model id. Used to attach a
    # monetary cost to replay/comparison runs. Defaults cover the generation
    # model (gemini-3.5-flash) and the judge/investigator model (gemini-3.1-pro).
    model_pricing: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
            "gemini-3.5-flash": ModelPricing(
                prompt_usd_per_1k=0.000075, completion_usd_per_1k=0.0003
            ),
            "gemini-3.1-pro": ModelPricing(
                prompt_usd_per_1k=0.00125, completion_usd_per_1k=0.005
            ),
        },
        alias="RAG_MODEL_PRICING",
    )

    @field_validator("route_min_confidence", "retrieval_score_threshold")
    @classmethod
    def _validate_confidence_unit_interval(cls, value: float) -> float:
        """Confidence/score thresholds must lie in the inclusive [0.0, 1.0]."""
        if value < 0.0 or value > 1.0:
            raise ValueError(
                f"invalid confidence threshold {value!r}: must be within the "
                "inclusive range [0.0, 1.0]"
            )
        return value

    @field_validator("corpus_page_size")
    @classmethod
    def _validate_corpus_page_size(cls, value: int) -> int:
        """Page size must be a positive integer no larger than 100 (R4.4)."""
        if value < 1 or value > 100:
            raise ValueError(
                f"invalid corpus page size {value!r}: must be within 1 and 100 inclusive"
            )
        return value

    @field_validator(
        "llm_judge_thinking_budget",
        "trace_investigator_thinking_budget",
    )
    @classmethod
    def _validate_thinking_budget(cls, value: int) -> int:
        """Thinking-token budgets are bounded to a sane [0, 32768] range.

        Zero disables thinking; the upper bound guards against a runaway budget
        that would blow the per-case timeout and cost.
        """
        if value < 0 or value > 32768:
            raise ValueError(
                f"invalid thinking budget {value!r}: must be within 0 and 32768 inclusive"
            )
        return value

    @field_validator(
        "clarification_expiry_minutes",
        "retrieval_metric_depth_k",
        "llm_judge_read_timeout_s",
        "llm_judge_per_case_timeout_s",
        "llm_judge_schedule_interval_hours",
        "trace_investigator_read_timeout_s",
        "knowledge_gap_max_topics",
        "knowledge_gap_min_eligible_outcomes",
        "replay_job_timeout_s",
    )
    @classmethod
    def _validate_positive_bounded_minutes_counts(cls, value: int) -> int:
        """Positive time windows / counts must fall within [1, 100000].

        A zero/negative value would disable or invert the knob it controls, and
        an absurdly large one defeats the bound it exists to enforce.
        """
        if value < 1 or value > 100_000:
            raise ValueError(
                f"invalid value {value!r}: must be within 1 and 100000 inclusive"
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
    # Comma-separated allow-list of operator email addresses. A user whose email
    # is in this list is treated as an operator at request time even without the
    # stored ``is_operator`` flag, bootstrapping the first operators without a
    # migration or admin UI. Empty by default (no allow-listed operators).
    operator_emails: str = Field(default="", alias="RAG_OPERATOR_EMAILS")

    @property
    def operator_emails_set(self) -> frozenset[str]:
        """Normalized (stripped, lowercased) set of allow-listed operator emails."""
        return frozenset(
            email.strip().lower()
            for email in self.operator_emails.split(",")
            if email.strip()
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

    def gcs_client(self):
        """Return a Google Cloud Storage client using Application Default
        Credentials (the VM/service-account identity that already backs Vertex
        AI). ``google_application_credentials`` overrides ADC when set."""
        from google.cloud import storage

        if self.google_application_credentials:
            return storage.Client.from_service_account_json(
                self.google_application_credentials, project=self.gcp_project_id
            )
        return storage.Client(project=self.gcp_project_id)

    def pubsub_publisher(self):
        """Return a Pub/Sub PublisherClient (ADC-authenticated)."""
        from google.cloud import pubsub_v1

        return pubsub_v1.PublisherClient()

    def pubsub_subscriber(self):
        """Return a Pub/Sub SubscriberClient (ADC-authenticated)."""
        from google.cloud import pubsub_v1

        return pubsub_v1.SubscriberClient()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
