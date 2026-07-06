"""Dedicated Gemini structured-output client for the SQL Lab auto-dashboard.

The shared ``TextLLM.generate`` interface (:mod:`rag_system.llm`) accepts only
``(prompt, *, temperature, max_tokens, thinking_budget)``. It exposes **no**
``response_schema``/``response_mime_type`` parameter (so it cannot request the
schema-constrained structured JSON required by R9.4) and **no** per-call model
override (its model id is fixed at construction, so it cannot switch between
Flash and Pro per request, required by R9.8/R9.9). Rather than widen that shared
interface for a single caller, this module owns a small dedicated
``google-genai`` client that:

* builds a Vertex AI ``genai.Client`` with a 60-second ``HttpOptions`` timeout,
  so a slow or unavailable model surfaces as a client error the analyzer/route
  can map to the R9.10 "analysis could not be completed" error;
* runs ``models.generate_content`` with a ``GenerateContentConfig`` carrying
  ``response_mime_type="application/json"`` and
  ``response_schema=CHART_SPEC_RESPONSE_SCHEMA`` (R9.4); and
* selects the model id per :data:`AnalysisMode` from ``Settings`` — the Flash
  model (``sql_lab_analysis_model_id``, default ``gemini-3.5-flash``) for the
  default mode and the Pro model (``sql_lab_deep_analysis_model_id``, default
  ``gemini-3.1-pro``) for the deep mode (R9.8/R9.9).

The ``google-genai`` SDK is imported lazily inside methods (mirroring
:class:`rag_system.llm.GeminiTextLLM` and :class:`rag_system.embedding.GeminiEmbedder`)
so importing this module never fails in environments without the optional
dependency installed. Building the payload/prompt for the model is the
responsibility of the :class:`ChartSpecAnalyzer` (task 12.4); this client is a
thin transport that returns the raw response text unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from rag_system.config import Settings
from rag_system.sql_lab.chart_spec import CHART_SPEC_RESPONSE_SCHEMA

#: The analysis modes SQL Lab supports. ``"default"`` maps to the Gemini Flash
#: model id; ``"deep"`` maps to the Gemini Pro model id (R9.8/R9.9).
AnalysisMode = Literal["default", "deep"]

#: The HTTP budget for a single structured-output call, in milliseconds. The
#: 60-second analysis budget (R9.10) is enforced here at the client level.
_HTTP_TIMEOUT_MS = 60_000


class SqlLabGeminiClient:
    """A dedicated ``google-genai`` structured-output client for Chart_Spec generation.

    Construction builds a Vertex AI ``genai.Client`` with a 60s ``HttpOptions``
    timeout and captures the per-mode model ids from ``Settings``.
    :meth:`generate_chart_spec_json` runs a single schema-constrained
    ``generate_content`` call and returns the raw response text; validating that
    text against the strict :class:`~rag_system.sql_lab.chart_spec.ChartSpec`
    schema is the caller's responsibility (R9.5).
    """

    def __init__(self, settings: Settings) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "google-genai is not installed. Run `pip install google-genai` "
                "to use the SQL Lab auto-dashboard analysis client."
            ) from exc

        if not settings.gcp_project_id:
            raise RuntimeError(
                "GCP_PROJECT_ID must be set to use the SQL Lab analysis client "
                "on Vertex AI."
            )

        # Honour an explicit service-account key path if provided; otherwise the
        # SDK falls back to Application Default Credentials (mirrors GeminiTextLLM).
        if settings.google_application_credentials:
            os.environ.setdefault(
                "GOOGLE_APPLICATION_CREDENTIALS",
                settings.google_application_credentials,
            )

        self._types = types
        # 60s HTTP budget enforced at the client level → maps to the R9.10 error path.
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS),  # milliseconds
        )
        # Per-mode model-id mapping, read from Settings (R9.8/R9.9).
        self._flash_model = settings.sql_lab_analysis_model_id  # default mode
        self._pro_model = settings.sql_lab_deep_analysis_model_id  # deep mode

    def _model_for_mode(self, mode: AnalysisMode) -> str:
        """Return the Gemini model id for ``mode`` (Pro for deep, else Flash)."""
        return self._pro_model if mode == "deep" else self._flash_model

    def generate_chart_spec_json(
        self, contents: Any, mode: AnalysisMode = "default"
    ) -> str:
        """Run a schema-constrained ``generate_content`` call and return raw text.

        ``contents`` is the prompt/payload assembled by the caller (task 12.4).
        The model id is selected from ``mode`` (R9.8/R9.9) and generation is
        constrained to the declarative Chart_Spec shape via
        ``response_mime_type="application/json"`` +
        ``response_schema=CHART_SPEC_RESPONSE_SCHEMA`` (R9.4). Returns the raw
        response text for the caller to validate (R9.5); a slow or unavailable
        model raises through the client's 60s HTTP timeout (R9.10).
        """
        response = self._client.models.generate_content(
            model=self._model_for_mode(mode),
            contents=contents,
            config=self._types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CHART_SPEC_RESPONSE_SCHEMA,
            ),
        )
        return response.text


__all__ = [
    "AnalysisMode",
    "SqlLabGeminiClient",
]
