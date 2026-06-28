"""Property tests for tracing sample-rate configuration validation.

Feature: ai-observability-platform.

These tests exercise the startup validation added to ``Settings`` in
``src/rag_system/config.py`` (task 1.1): the ``trace_sample_rate`` field must be
a number within the inclusive range ``[0.0, 1.0]``. A non-numeric or
out-of-range value must be rejected at startup, surfaced as a pydantic
``ValidationError`` through the same construction path used by
``get_settings()`` (R10.6).

The ``Settings`` model uses field *aliases* (e.g. ``RAG_TRACE_SAMPLE_RATE``), so
all values are supplied by alias here exactly as they would arrive from the
environment. The other required settings are supplied with throwaway values so
the model can be constructed in isolation without depending on a ``.env`` file.
"""

import math
import string

import pydantic
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.config import Settings

# ---------------------------------------------------------------------------
# Construction helper - supply the required settings by alias so the model can
# be built in isolation, overriding only the sample rate under test.
# ---------------------------------------------------------------------------

_REQUIRED_BY_ALIAS = {
    "RAG_S3_BUCKET": "test-bucket",
    "RAG_INGESTION_QUEUE_URL": "https://sqs.example/queue",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(sample_rate: object) -> Settings:
    """Construct ``Settings`` with the given raw ``RAG_TRACE_SAMPLE_RATE`` value."""
    return Settings(RAG_TRACE_SAMPLE_RATE=sample_rate, **_REQUIRED_BY_ALIAS)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Smart generators - constrained to the input domain that R10.6 describes.
# ---------------------------------------------------------------------------

# Valid: any real, finite sampling rate within the inclusive unit interval.
_valid_rates = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


def _not_floatable(text: str) -> bool:
    """True when ``text`` cannot be coerced to a float (genuinely non-numeric)."""
    try:
        float(text)
    except ValueError:
        return True
    return False


# Invalid - finite numbers strictly outside the inclusive [0.0, 1.0] interval.
_out_of_range_numbers = st.floats(allow_nan=False, allow_infinity=False).filter(
    lambda x: x < 0.0 or x > 1.0
)

# Invalid - the unbounded extremes, which are unambiguously outside [0.0, 1.0].
_infinities = st.sampled_from([math.inf, -math.inf])

# Invalid - non-numeric strings (letters/spaces that do not parse as a number).
_non_numeric_strings = st.text(
    alphabet=string.ascii_letters + " ", min_size=1, max_size=16
).filter(_not_floatable)

# Invalid - structurally wrong types that cannot represent a sampling rate.
_wrong_types = st.one_of(
    st.none(),
    st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=4),
    st.dictionaries(st.text(max_size=4), st.integers(), min_size=1, max_size=3),
)

_invalid_rates = st.one_of(
    _out_of_range_numbers,
    _infinities,
    _non_numeric_strings,
    _wrong_types,
)


# ---------------------------------------------------------------------------
# Property 22 - invalid sample-rate configuration is rejected at startup.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 22: Invalid sample-rate configuration is rejected at startup
# Validates: Requirements 10.6
@settings(max_examples=100)
@given(sample_rate=_invalid_rates)
def test_invalid_sample_rate_is_rejected_at_startup(sample_rate: object) -> None:
    """Any non-numeric or out-of-range sampling rate fails at construction.

    R10.6: a configured sampling rate that is non-numeric or outside the
    inclusive range [0.0, 1.0] must be rejected at startup with an error - here
    a pydantic ``ValidationError`` raised through the ``Settings`` construction
    path used by ``get_settings()``.
    """
    with pytest.raises(pydantic.ValidationError):
        _build_settings(sample_rate)


# Feature: ai-observability-platform, Property 22: Invalid sample-rate configuration is rejected at startup
# Validates: Requirements 10.6
@settings(max_examples=100)
@given(sample_rate=_valid_rates)
def test_valid_sample_rate_is_accepted_at_startup(sample_rate: float) -> None:
    """Any real, finite rate within [0.0, 1.0] is accepted and preserved.

    This is the complementary direction of R10.6: valid configuration must not
    be rejected, and the accepted value must round-trip onto the settings field.
    """
    config = _build_settings(sample_rate)
    assert config.trace_sample_rate == sample_rate
    assert 0.0 <= config.trace_sample_rate <= 1.0
