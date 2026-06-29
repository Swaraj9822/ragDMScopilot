"""Text-generation LLM abstraction.

Provides a single ``TextLLM`` interface backed by Google Gemini on
Vertex AI.

All callers (RAG generation, query routing, database copilot) depend only
on :func:`build_text_llm` and the :class:`TextLLM` protocol, so the
underlying model can change without touching business logic.
"""

from __future__ import annotations

import os
from typing import Any, Iterator, Protocol, runtime_checkable

from rag_system.config import Settings
from rag_system.observability import add_tokens, get_logger

logger = get_logger(__name__)


@runtime_checkable
class TextLLM(Protocol):
    """Minimal text-in / text-out chat completion interface."""

    model_id: str
    provider: str

    def generate(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Return ``(text, usage)`` for a single-prompt completion.

        ``thinking_budget`` is honoured only by reasoning-capable providers
        (Gemini) and ignored elsewhere.
        """
        ...

    def generate_stream(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> Iterator[str]:
        """Yield incremental text chunks for a single-prompt completion.

        Streams the model's tokens as they are produced so callers can forward
        them to the client. Token usage is tallied into the per-request counter
        when the provider reports it.
        """
        ...


class GeminiTextLLM:
    """Text generation via Google Gemini on Vertex AI (``google-genai`` SDK)."""

    provider = "gemini"

    def __init__(self, settings: Settings) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "google-genai is not installed. Run `pip install google-genai` "
                "to use the Gemini LLM provider."
            ) from exc

        if not settings.gcp_project_id:
            raise RuntimeError(
                "GCP_PROJECT_ID must be set to use the Gemini provider on Vertex AI."
            )

        # Honour an explicit service-account key path if provided; otherwise the
        # SDK falls back to Application Default Credentials.
        if settings.google_application_credentials:
            os.environ.setdefault(
                "GOOGLE_APPLICATION_CREDENTIALS",
                settings.google_application_credentials,
            )

        self._types = types
        self._default_thinking_budget = settings.gemini_thinking_budget
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            http_options=types.HttpOptions(
                timeout=int(settings.gemini_read_timeout_s * 1000)
            ),
        )
        self.model_id = settings.gemini_model_id

    def generate(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        budget = thinking_budget if thinking_budget is not None else self._default_thinking_budget
        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if budget is not None:
            config_kwargs["thinking_config"] = self._types.ThinkingConfig(
                thinking_budget=budget
            )
        response = self._client.models.generate_content(
            model=self.model_id,
            contents=prompt,
            config=self._types.GenerateContentConfig(**config_kwargs),
        )
        text = response.text or ""
        usage: dict[str, Any] = {}
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            mapped = {
                "inputTokens": getattr(meta, "prompt_token_count", None),
                "outputTokens": getattr(meta, "candidates_token_count", None),
                "thoughtsTokens": getattr(meta, "thoughts_token_count", None),
                "totalTokens": getattr(meta, "total_token_count", None),
            }
            usage = {key: value for key, value in mapped.items() if value is not None}
        # Surface the model's average token log-probability when the response
        # carries it (only present when logprobs were requested). This feeds
        # the numeric confidence score; absent it, confidence falls back to
        # grounding signals. Best-effort and never fatal.
        avg_logprob = self._extract_avg_logprob(response)
        if avg_logprob is not None:
            usage["avgLogprob"] = avg_logprob
        # Tally this call's tokens into the per-request total so the whole
        # request's token cost can be reported on its trace (R: token totals).
        add_tokens(usage.get("totalTokens"))
        return text, usage

    @staticmethod
    def _extract_avg_logprob(response: Any) -> float | None:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            value = getattr(candidate, "avg_logprobs", None)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def generate_stream(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> Iterator[str]:
        budget = thinking_budget if thinking_budget is not None else self._default_thinking_budget
        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if budget is not None:
            config_kwargs["thinking_config"] = self._types.ThinkingConfig(
                thinking_budget=budget
            )
        stream = self._client.models.generate_content_stream(
            model=self.model_id,
            contents=prompt,
            config=self._types.GenerateContentConfig(**config_kwargs),
        )
        total_tokens: int | None = None
        for chunk in stream:
            text = getattr(chunk, "text", None) or ""
            if text:
                yield text
            # usage_metadata is cumulative across chunks; the final value wins.
            meta = getattr(chunk, "usage_metadata", None)
            if meta is not None:
                value = getattr(meta, "total_token_count", None)
                if value is not None:
                    total_tokens = value
        # Fold the whole call's tokens into the per-request tally once complete.
        add_tokens(total_tokens)


def build_text_llm(settings: Settings) -> TextLLM:
    """Construct the Gemini text-generation LLM."""
    client = GeminiTextLLM(settings)
    logger.info(
        "Using Gemini LLM provider (model=%s, project=%s, location=%s)",
        client.model_id,
        settings.gcp_project_id,
        settings.gcp_location,
    )
    return client
