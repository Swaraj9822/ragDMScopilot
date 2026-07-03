"""Property test for trace config recording with redaction (R9.1, R9.11).

# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted

Validates that :func:`redact_settings` and :func:`build_trace_config_payload`
honour the redaction contract:

- *For any* settings dict with keys matching sensitive patterns (api_key,
  secret, token, credential, password), the output replaces those values with
  the redaction placeholder — including in nested dicts (retrieval_settings,
  reranker_config).
- *For any* settings dict with non-sensitive keys, those values are preserved
  unchanged in the output.
- *For any* settings dict provided as input, the source dict is never mutated.
- *For any* unresolved config, the payload records the unresolved version_id
  sentinel and empty settings.

**Validates: Requirements 9.1, 9.11**
"""

from __future__ import annotations

import copy
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.ai_config import UNRESOLVED_VERSION_ID, ResolvedConfig
from rag_system.observability_tracing.config_redaction import (
    REDACTED_PLACEHOLDER,
    SENSITIVE_KEY_PATTERNS,
    build_trace_config_payload,
    redact_settings,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Keys that always match at least one sensitive pattern (case-insensitive substring).
_sensitive_key_strategy = st.one_of(
    # Use each pattern as a substring inside a key name
    st.sampled_from(list(SENSITIVE_KEY_PATTERNS)).flatmap(
        lambda pat: st.tuples(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc")),
                min_size=0,
                max_size=5,
            ),
            st.just(pat),
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc")),
                min_size=0,
                max_size=5,
            ),
        ).map(lambda parts: parts[0] + parts[1] + parts[2])
    ),
    # Mixed-case variants
    st.sampled_from(list(SENSITIVE_KEY_PATTERNS)).flatmap(
        lambda pat: st.just(pat.upper())
    ),
    st.sampled_from(list(SENSITIVE_KEY_PATTERNS)).flatmap(
        lambda pat: st.just(pat.title())
    ),
)

# Keys that do NOT match any sensitive pattern.
_non_sensitive_key_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc")),
    min_size=1,
    max_size=20,
).filter(
    lambda k: not any(pat in k.lower() for pat in SENSITIVE_KEY_PATTERNS)
)

# Arbitrary JSON-like leaf values.
_leaf_values = st.one_of(
    st.text(min_size=0, max_size=50),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    st.lists(st.integers(min_value=0, max_value=100), max_size=5),
)

# A flat dict with a mix of sensitive and non-sensitive keys.
_mixed_settings = st.fixed_dictionaries(
    {},
    optional={},
).flatmap(
    lambda _: st.tuples(
        st.dictionaries(
            keys=_sensitive_key_strategy,
            values=_leaf_values,
            min_size=1,
            max_size=5,
        ),
        st.dictionaries(
            keys=_non_sensitive_key_strategy,
            values=_leaf_values,
            min_size=1,
            max_size=5,
        ),
    ).map(lambda pair: {**pair[0], **pair[1]})
)

# A nested settings dict simulating retrieval_settings / reranker_config.
_nested_settings = st.fixed_dictionaries(
    {},
    optional={},
).flatmap(
    lambda _: st.tuples(
        st.dictionaries(
            keys=_sensitive_key_strategy,
            values=_leaf_values,
            min_size=0,
            max_size=3,
        ),
        st.dictionaries(
            keys=_non_sensitive_key_strategy,
            values=_leaf_values,
            min_size=1,
            max_size=3,
        ),
        st.dictionaries(
            keys=_sensitive_key_strategy,
            values=_leaf_values,
            min_size=0,
            max_size=3,
        ),
        st.dictionaries(
            keys=_non_sensitive_key_strategy,
            values=_leaf_values,
            min_size=1,
            max_size=3,
        ),
    ).map(
        lambda parts: {
            "prompt": "test prompt",
            "model": "gemini-3.5-flash",
            **parts[0],
            **parts[1],
            "retrieval_settings": {**parts[2], "top_k": 20},
            "reranker_config": {**parts[3], "enabled": True},
        }
    )
)


def _is_sensitive(key: str) -> bool:
    """Check if a key matches any sensitive pattern (case-insensitive substring)."""
    lower = key.lower()
    return any(pat in lower for pat in SENSITIVE_KEY_PATTERNS)


def _check_redaction_recursive(original: dict[str, Any], redacted: dict[str, Any]) -> None:
    """Recursively verify redaction rules on a pair of original/redacted dicts."""
    for key in original:
        assert key in redacted, f"Key {key!r} missing from redacted output"
        if _is_sensitive(key):
            assert redacted[key] == REDACTED_PLACEHOLDER, (
                f"Sensitive key {key!r} was not redacted: got {redacted[key]!r}"
            )
        elif isinstance(original[key], dict):
            assert isinstance(redacted[key], dict)
            _check_redaction_recursive(original[key], redacted[key])
        else:
            assert redacted[key] == original[key], (
                f"Non-sensitive key {key!r} was altered: expected {original[key]!r}, got {redacted[key]!r}"
            )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=200)
@given(settings_dict=_mixed_settings)
def test_sensitive_keys_are_redacted_in_flat_dict(settings_dict: dict[str, Any]) -> None:
    """All keys matching sensitive patterns are replaced with the redaction placeholder."""
    result = redact_settings(settings_dict)
    for key, value in result.items():
        if _is_sensitive(key):
            assert value == REDACTED_PLACEHOLDER


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=200)
@given(settings_dict=_mixed_settings)
def test_non_sensitive_keys_preserved_in_flat_dict(settings_dict: dict[str, Any]) -> None:
    """Non-sensitive keys retain their original values unchanged."""
    result = redact_settings(settings_dict)
    for key in settings_dict:
        if not _is_sensitive(key):
            assert result[key] == settings_dict[key]


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=200)
@given(settings_dict=_mixed_settings)
def test_source_dict_never_mutated(settings_dict: dict[str, Any]) -> None:
    """The source dict is never mutated by redact_settings."""
    original = copy.deepcopy(settings_dict)
    redact_settings(settings_dict)
    assert settings_dict == original


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=200)
@given(settings_dict=_nested_settings)
def test_nested_dicts_redacted_recursively(settings_dict: dict[str, Any]) -> None:
    """Sensitive keys inside nested dicts (retrieval_settings, reranker_config) are redacted."""
    result = redact_settings(settings_dict)
    _check_redaction_recursive(settings_dict, result)


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=200)
@given(settings_dict=_nested_settings)
def test_source_dict_never_mutated_nested(settings_dict: dict[str, Any]) -> None:
    """The source dict with nested structure is never mutated."""
    original = copy.deepcopy(settings_dict)
    redact_settings(settings_dict)
    assert settings_dict == original


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=100)
@given(
    version_id=st.text(min_size=1, max_size=40, alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc", "Pd"))),
    settings_dict=_nested_settings,
)
def test_build_trace_config_payload_resolved(version_id: str, settings_dict: dict[str, Any]) -> None:
    """A resolved config produces a payload with version_id and fully redacted settings."""
    rc = ResolvedConfig(
        version_id=version_id,
        config_id="default",
        prompt=settings_dict.get("prompt", "test"),
        model=settings_dict.get("model", "gemini-3.5-flash"),
        output_schema=settings_dict.get("output_schema", {}),
        router_threshold=settings_dict.get("router_threshold", 0.5),
        retrieval_settings=settings_dict.get("retrieval_settings", {}),
        reranker_config=settings_dict.get("reranker_config", {}),
        is_resolved=True,
    )
    original_retrieval = copy.deepcopy(rc.retrieval_settings)
    original_reranker = copy.deepcopy(rc.reranker_config)

    payload = build_trace_config_payload(rc)

    # Version id is stamped
    assert payload["ai_configuration_version_id"] == version_id

    # Resolved settings are present and redacted
    rs = payload["resolved_settings"]
    assert rs["prompt"] == rc.prompt
    assert rs["model"] == rc.model

    # Sensitive keys in retrieval_settings are redacted
    for key in rs.get("retrieval_settings", {}):
        if _is_sensitive(key):
            assert rs["retrieval_settings"][key] == REDACTED_PLACEHOLDER

    # Sensitive keys in reranker_config are redacted
    for key in rs.get("reranker_config", {}):
        if _is_sensitive(key):
            assert rs["reranker_config"][key] == REDACTED_PLACEHOLDER

    # Source never mutated
    assert rc.retrieval_settings == original_retrieval
    assert rc.reranker_config == original_reranker


# Feature: rag-trust-and-observability, Property 31: Traces record the producing configuration version with secrets redacted
# Validates: Requirements 9.1, 9.11
@settings(max_examples=100)
@given(
    config_id=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc"))),
)
def test_build_trace_config_payload_unresolved(config_id: str) -> None:
    """An unresolved config records the unresolved version_id with empty settings."""
    rc = ResolvedConfig.unresolved(config_id)
    payload = build_trace_config_payload(rc)

    assert payload["ai_configuration_version_id"] == UNRESOLVED_VERSION_ID
    assert payload["resolved_settings"] == {}
