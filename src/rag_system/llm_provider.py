"""Backend-agnostic generation provider abstraction.

Defines the protocol, request/result data types, and provider implementations
for invoking large language models (Bedrock, Gemini) through a unified interface.
All four generation call sites obtain text exclusively through GenerationProvider.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from rag_system.config import Settings
from rag_system.observability import (
    CircuitOpenError,
    get_circuit_breaker,
    get_logger,
    get_trace_id,
    metrics,
    retry_on_transient,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class GenerationRequest:
    """Provider-agnostic input for a text-generation call.

    Attributes:
        user_prompt: The user-facing prompt text.
        system_prompt: Optional system-level instruction (used by Copilot call sites).
        temperature: Sampling temperature; lower values produce more deterministic output.
        max_output_tokens: Maximum number of tokens to generate.
    """

    user_prompt: str
    system_prompt: str | None = None
    temperature: float = 0.1
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class GenerationResult:
    """Provider-agnostic output of a text-generation call.

    Attributes:
        text: The generated text content.
        usage: Token usage counts keyed by type,
            e.g. {"inputTokens": .., "outputTokens": .., "totalTokens": ..}.
    """

    text: str
    usage: dict[str, int] = field(default_factory=dict)


class GenerationProvider(Protocol):
    """Backend-agnostic protocol for invoking a generation model.

    Implementations (BedrockProvider, GeminiProvider) handle SDK-specific
    request mapping, resilience (retry + circuit breaker), metrics, and logging.
    """

    name: str
    """Circuit-breaker / metric key, e.g. "bedrock" or "gemini"."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Invoke the model with the given request and return the result.

        Raises on unrecoverable errors after retries are exhausted.
        """
        ...

    def readiness_check(self) -> None:
        """Verify client construction and configuration WITHOUT invoking the model.

        Raises on misconfiguration (missing credentials, unreachable endpoint, etc.).
        """
        ...


class BedrockProvider:
    """Generation provider backed by the AWS Bedrock Converse API.

    Applies retry-on-transient with exponential backoff and routes through a
    named circuit breaker for fail-fast behavior on repeated failures.
    """

    name = "bedrock"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = settings.boto3_session().client(
            "bedrock-runtime", config=settings.bedrock_botocore_config()
        )
        self._model_id = settings.bedrock_model_id
        self._cb_threshold = settings.circuit_failure_threshold
        self._cb_recovery = settings.circuit_recovery_timeout_s

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Circuit-protected generation via Bedrock Converse API."""
        cb = get_circuit_breaker("bedrock", self._cb_threshold, self._cb_recovery)
        if not cb.allow_request():
            opened_ago = time.perf_counter() - cb._opened_at if cb._opened_at else 0.0
            raise CircuitOpenError("bedrock", opened_ago)
        try:
            result = self._generate_inner(request)
        except Exception:
            cb.record_failure()
            raise
        else:
            cb.record_success()
            # Emit token usage metrics
            for token_type, count in result.usage.items():
                if isinstance(count, (int, float)):
                    metrics.observe(
                        "rag_generation_tokens",
                        float(count),
                        {"model_id": self._model_id, "token_type": token_type},
                    )
            # Structured log
            logger.info(
                "Generation completed (provider=%s, model=%s, trace=%s)",
                self.name,
                self._model_id,
                get_trace_id(),
                extra={"model_id": self._model_id, "trace_id": get_trace_id()},
            )
            return result

    @retry_on_transient()
    def _generate_inner(self, request: GenerationRequest) -> GenerationResult:
        """Call Bedrock Converse with retry on transient failures."""
        kwargs: dict = {
            "modelId": self._model_id,
            "messages": [{"role": "user", "content": [{"text": request.user_prompt}]}],
            "inferenceConfig": {
                "temperature": request.temperature,
                "maxTokens": request.max_output_tokens,
            },
        }
        if request.system_prompt:
            kwargs["system"] = [{"text": request.system_prompt}]
        response = self._client.converse(**kwargs)
        text = response["output"]["message"]["content"][0]["text"]
        return GenerationResult(text=text, usage=response.get("usage", {}))

    def readiness_check(self) -> None:
        """Verify Bedrock client construction without invoking the model."""
        self._settings.boto3_session().client(
            "bedrock-runtime", config=self._settings.bedrock_botocore_config()
        )


# ---------------------------------------------------------------------------
# Gemini transient error types
# ---------------------------------------------------------------------------

try:
    from google.api_core.exceptions import (
        DeadlineExceeded,
        InternalServerError,
        ResourceExhausted,
        ServiceUnavailable,
        TooManyRequests,
    )

    _GEMINI_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
        ServiceUnavailable,
        TooManyRequests,
        ResourceExhausted,
        DeadlineExceeded,
        InternalServerError,
        ConnectionError,
        TimeoutError,
    )
except ImportError:
    _GEMINI_TRANSIENT_ERRORS = (ConnectionError, TimeoutError)


# ---------------------------------------------------------------------------
# Gemini usage normalization
# ---------------------------------------------------------------------------


def _normalize_usage(usage_metadata) -> dict[str, int]:
    """Map Vertex AI usage_metadata to the standard token-usage dict."""
    if usage_metadata is None:
        return {}
    return {
        "inputTokens": getattr(usage_metadata, "prompt_token_count", 0) or 0,
        "outputTokens": getattr(usage_metadata, "candidates_token_count", 0) or 0,
        "totalTokens": getattr(usage_metadata, "total_token_count", 0) or 0,
    }


# ---------------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------------


class GeminiProvider:
    """Generation provider backed by Google Gemini through GCP Vertex AI.

    Applies retry-on-transient with exponential backoff (using Gemini-specific
    transient error types) and routes through a named circuit breaker for
    fail-fast behavior on repeated failures.
    """

    name = "gemini"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._model_id = settings.gemini_model_id
        self._project = settings.gcp_project_id
        self._location = settings.gcp_location
        self._read_timeout_s = settings.gemini_read_timeout_s
        self._cb_threshold = settings.circuit_failure_threshold
        self._cb_recovery = settings.circuit_recovery_timeout_s
        self._initialized = False  # lazy vertexai.init

    def _ensure_initialized(self) -> None:
        """Lazily import vertexai and initialize the SDK.

        Raises RuntimeError with actionable guidance if the library is missing
        or the required GCP_PROJECT_ID is not configured.
        """
        if self._initialized:
            return
        try:
            import vertexai  # noqa: F811
        except ImportError as e:
            raise RuntimeError(
                "google-cloud-aiplatform is required for the Gemini provider. "
                "Install it with: pip install 'google-cloud-aiplatform>=1.60.0'"
            ) from e
        if not self._project:
            raise RuntimeError(
                "GCP_PROJECT_ID is required when LLM_PROVIDER=gemini. "
                "Set the GCP_PROJECT_ID environment variable."
            )
        vertexai.init(project=self._project, location=self._location)
        self._initialized = True

    def _build_model(self, system_instruction: str | None = None):
        """Construct a GenerativeModel, lazily initializing the SDK if needed."""
        self._ensure_initialized()
        from vertexai.generative_models import GenerativeModel

        kwargs: dict = {"model_name": self._model_id}
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        return GenerativeModel(**kwargs)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Circuit-protected generation via Vertex AI Gemini."""
        cb = get_circuit_breaker("gemini", self._cb_threshold, self._cb_recovery)
        if not cb.allow_request():
            opened_ago = time.perf_counter() - cb._opened_at if cb._opened_at else 0.0
            raise CircuitOpenError("gemini", opened_ago)
        try:
            result = self._generate_inner(request)
        except Exception:
            cb.record_failure()
            raise
        else:
            cb.record_success()
            # Emit token usage metrics
            for token_type, count in result.usage.items():
                if isinstance(count, (int, float)):
                    metrics.observe(
                        "rag_generation_tokens",
                        float(count),
                        {"model_id": self._model_id, "token_type": token_type},
                    )
            # Structured log
            logger.info(
                "Generation completed (provider=%s, model=%s, trace=%s)",
                self.name,
                self._model_id,
                get_trace_id(),
                extra={"model_id": self._model_id, "trace_id": get_trace_id()},
            )
            return result

    @retry_on_transient(retryable_exceptions=_GEMINI_TRANSIENT_ERRORS)
    def _generate_inner(self, request: GenerationRequest) -> GenerationResult:
        """Call Vertex AI Gemini with retry on transient failures."""
        from vertexai.generative_models import GenerationConfig

        model = self._build_model(system_instruction=request.system_prompt)
        config = GenerationConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
        )
        response = model.generate_content(
            request.user_prompt,
            generation_config=config,
        )
        return GenerationResult(text=response.text, usage=_normalize_usage(response.usage_metadata))

    def readiness_check(self) -> None:
        """Verify import, project configuration, and SDK init without a model call."""
        self._ensure_initialized()


# ---------------------------------------------------------------------------
# Combined transient errors for fallback detection
# ---------------------------------------------------------------------------

try:
    from botocore.exceptions import BotoCoreError

    _ALL_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
        *_GEMINI_TRANSIENT_ERRORS,
        BotoCoreError,
    )
except ImportError:
    _ALL_TRANSIENT_ERRORS = _GEMINI_TRANSIENT_ERRORS


# ---------------------------------------------------------------------------
# FallbackProvider
# ---------------------------------------------------------------------------


class FallbackProvider:
    """Wraps a primary provider with fallback to a secondary on non-transient failure.

    Transient errors and CircuitOpenError are re-raised (already retried by the primary);
    all other exceptions trigger a fallback attempt through the secondary provider.
    A metric and structured log entry are emitted on each fallback.
    """

    def __init__(self, primary: GenerationProvider, secondary: GenerationProvider):
        self._primary = primary
        self._secondary = secondary

    @property
    def name(self) -> str:
        """Delegate identity to the primary provider."""
        return self._primary.name

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Attempt generation through the primary; fall back to secondary on non-transient failure.

        - CircuitOpenError is always re-raised (circuit is open, no point falling back).
        - Transient errors are re-raised (already retried by the primary's retry layer).
        - Any other (non-transient) exception triggers fallback: a metric is incremented,
          a structured warning is logged, and the request is retried via the secondary.
        """
        try:
            return self._primary.generate(request)
        except CircuitOpenError:
            raise
        except _ALL_TRANSIENT_ERRORS:
            raise
        except Exception:
            metrics.increment(
                "rag_generation_provider_fallback_total",
                {"from": self._primary.name, "to": self._secondary.name},
            )
            logger.warning(
                "Provider fallback: %s -> %s",
                self._primary.name,
                self._secondary.name,
                extra={
                    "model_id": getattr(self._primary, "_model_id", self._primary.name),
                    "trace_id": get_trace_id(),
                },
            )
            return self._secondary.generate(request)

    def readiness_check(self) -> None:
        """Readiness delegates to the primary provider."""
        self._primary.readiness_check()


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def get_generation_provider(settings: Settings) -> GenerationProvider:
    """Construct the active generation provider based on configuration.

    Returns GeminiProvider when llm_provider == "gemini", optionally wrapped in
    FallbackProvider(primary, BedrockProvider) when llm_fallback_to_bedrock is true.
    Otherwise returns BedrockProvider.
    """
    if settings.llm_provider == "gemini":
        primary = GeminiProvider(settings)
        if settings.llm_fallback_to_bedrock:
            return FallbackProvider(primary, BedrockProvider(settings))
        return primary
    return BedrockProvider(settings)
