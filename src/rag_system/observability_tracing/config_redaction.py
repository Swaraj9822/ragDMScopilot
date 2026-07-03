"""Configuration redaction for trace recording (R9.11).

Provides :func:`redact_settings` which produces a deep copy of a configuration
settings dictionary with all sensitive values replaced by a redaction
placeholder. Sensitivity is determined by key-name substring matching against a
fixed set of patterns (``api_key``, ``secret``, ``token``, ``credential``,
``password``), applied case-insensitively. The function never mutates its input.

The companion :func:`build_trace_config_payload` integrates with
:class:`~rag_system.ai_config.ResolvedConfig` to produce the complete
trace-facing projection of an AI configuration version — version_id plus
redacted settings — ready to be stamped on a :class:`~.models.Trace`.
"""

from __future__ import annotations

import copy
from typing import Any

__all__ = [
    "REDACTED_PLACEHOLDER",
    "SENSITIVE_KEY_PATTERNS",
    "build_trace_config_payload",
    "redact_settings",
]

#: The literal string that replaces a sensitive value in the trace projection.
REDACTED_PLACEHOLDER = "***REDACTED***"

#: Substrings (case-insensitive) that mark a configuration key as sensitive.
#: Matching is performed on the *lowercased* key against each pattern.
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "api_key",
    "secret",
    "token",
    "credential",
    "password",
)


def _is_sensitive_key(key: str) -> bool:
    """Return True when *key* matches any sensitive pattern (case-insensitive substring)."""
    lower = key.lower()
    return any(pattern in lower for pattern in SENSITIVE_KEY_PATTERNS)


def redact_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of *settings* with sensitive values replaced.

    The function:
    1. Deep-copies the input so the original is never mutated.
    2. Walks the copy recursively: for any key matching a sensitive pattern (as a
       case-insensitive substring), the corresponding value is replaced with
       :data:`REDACTED_PLACEHOLDER`.
    3. Recurses into nested ``dict`` values (e.g. ``retrieval_settings``,
       ``reranker_config``) to redact credentials at any depth.

    Non-dict values at non-sensitive keys are preserved as-is.
    """
    copied = copy.deepcopy(settings)
    _redact_dict_inplace(copied)
    return copied


def _redact_dict_inplace(d: dict[str, Any]) -> None:
    """Recursively redact sensitive keys in *d* (mutating in-place on the copy)."""
    for key in list(d.keys()):
        if _is_sensitive_key(key):
            d[key] = REDACTED_PLACEHOLDER
        elif isinstance(d[key], dict):
            _redact_dict_inplace(d[key])


def build_trace_config_payload(resolved_config: Any) -> dict[str, Any]:
    """Build the trace-facing projection of a resolved AI configuration.

    Returns a dictionary containing:
    - ``ai_configuration_version_id``: the version identifier (or the
      ``"unresolved"`` sentinel when resolution failed).
    - ``resolved_settings``: the full settings bundle with sensitive values
      redacted. When the config is unresolved, this is an empty dict.

    The returned payload is safe for persistence in a trace: no secrets appear,
    and the source :class:`~rag_system.ai_config.ResolvedConfig` is never
    mutated.
    """
    from rag_system.ai_config import UNRESOLVED_VERSION_ID

    version_id: str = getattr(resolved_config, "version_id", UNRESOLVED_VERSION_ID)
    is_resolved: bool = getattr(resolved_config, "is_resolved", False)

    if not is_resolved:
        return {
            "ai_configuration_version_id": version_id,
            "resolved_settings": {},
        }

    # Collect the settings bundle into a dict for redaction.
    settings: dict[str, Any] = {
        "prompt": getattr(resolved_config, "prompt", ""),
        "model": getattr(resolved_config, "model", ""),
        "output_schema": getattr(resolved_config, "output_schema", {}),
        "router_threshold": getattr(resolved_config, "router_threshold", 0.5),
        "retrieval_settings": getattr(resolved_config, "retrieval_settings", {}),
        "reranker_config": getattr(resolved_config, "reranker_config", {}),
    }

    redacted = redact_settings(settings)

    return {
        "ai_configuration_version_id": version_id,
        "resolved_settings": redacted,
    }
