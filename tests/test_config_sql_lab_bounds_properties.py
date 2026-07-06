"""Property tests for SQL Lab Row_Limit / Statement_Timeout config bounds.

Feature: sql-lab.

These tests exercise the startup validation added to ``Settings`` in
``src/rag_system/config.py`` (task 1.1): ``sql_lab_row_limit`` must be an
integer within the inclusive range ``[1, 10000]`` and
``sql_lab_statement_timeout_ms`` must be an integer within the inclusive range
``[1, 60000]``. Any non-integer or out-of-range value must be rejected at
startup, surfaced as a pydantic ``ValidationError`` through the same
construction path used by ``get_settings()`` (R1.7, R1.8, R1.9), and the error
must name the offending configuration key.

The ``Settings`` model uses field *aliases* (``SQL_LAB_ROW_LIMIT`` /
``SQL_LAB_STATEMENT_TIMEOUT_MS``), so all values are supplied by alias here
exactly as they would arrive from the environment. The other required settings
are supplied with throwaway values so the model can be constructed in isolation
without depending on a ``.env`` file.
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
# be built in isolation, overriding only the field under test.
# ---------------------------------------------------------------------------

_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}

# Field-under-test descriptors: (alias, inclusive-min, inclusive-max, attr).
_ROW_LIMIT = ("SQL_LAB_ROW_LIMIT", 1, 10_000, "sql_lab_row_limit")
_TIMEOUT = ("SQL_LAB_STATEMENT_TIMEOUT_MS", 1, 60_000, "sql_lab_statement_timeout_ms")


def _build_settings(alias: str, value: object) -> Settings:
    """Construct ``Settings`` overriding only ``alias`` with ``value``."""
    return Settings(**{alias: value}, **_REQUIRED_BY_ALIAS)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Smart generators - constrained to the input domain R1.7-R1.9 describes.
# ---------------------------------------------------------------------------

# Valid integers span both fields' widest range so the same generator can be
# reused; each test constrains further to the field's own bounds.
_any_integer = st.integers(min_value=-1_000_000, max_value=1_000_000)


def _not_intable(text: str) -> bool:
    """True when ``text`` cannot be coerced to an int (genuinely non-integer)."""
    try:
        int(text)
    except ValueError:
        return True
    return False


# Invalid - floats with a genuine fractional part (integral floats like ``2.0``
# are excluded because pydantic legitimately coerces them to an int).
_fractional_floats = st.floats(
    allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6
).filter(lambda x: not x.is_integer())

# Invalid - the unbounded extremes, which cannot be a bounded integer.
_infinities = st.sampled_from([math.inf, -math.inf, math.nan])

# Invalid - non-numeric strings that do not parse as an integer.
_non_numeric_strings = st.text(
    alphabet=string.ascii_letters + " ", min_size=1, max_size=16
).filter(_not_intable)

# Invalid - structurally wrong types that cannot represent an integer bound.
_wrong_types = st.one_of(
    st.none(),
    st.lists(st.integers(), min_size=1, max_size=4),
    st.dictionaries(st.text(max_size=4), st.integers(), min_size=1, max_size=3),
)

_non_integer_values = st.one_of(
    _fractional_floats,
    _infinities,
    _non_numeric_strings,
    _wrong_types,
)


# ---------------------------------------------------------------------------
# Property 6 - Row_Limit and Statement_Timeout bounds are enforced at startup.
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 6: Row_Limit and Statement_Timeout config bounds are enforced at startup
# Validates: Requirements 1.7, 1.8, 1.9
@pytest.mark.parametrize("field", [_ROW_LIMIT, _TIMEOUT], ids=["row_limit", "timeout"])
@settings(max_examples=100)
@given(value=_any_integer)
def test_integer_accepted_iff_within_bounds(
    field: tuple[str, int, int, str], value: int
) -> None:
    """An integer is accepted iff it lies within the field's inclusive range.

    R1.7/R1.8: the row limit must be within [1, 10000] and the statement timeout
    within [1, 60000]. R1.9: an out-of-range value fails startup validation with
    an error naming the offending key. Both directions of the ``iff`` are
    checked here: in-range integers round-trip onto the settings field, and
    out-of-range integers raise a ``ValidationError`` that names the key.
    """
    alias, low, high, attr = field
    if low <= value <= high:
        config = _build_settings(alias, value)
        assert getattr(config, attr) == value
    else:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _build_settings(alias, value)
        assert alias in str(exc_info.value)


# Feature: sql-lab, Property 6: Row_Limit and Statement_Timeout config bounds are enforced at startup
# Validates: Requirements 1.7, 1.8, 1.9
@pytest.mark.parametrize("field", [_ROW_LIMIT, _TIMEOUT], ids=["row_limit", "timeout"])
@settings(max_examples=100)
@given(value=_non_integer_values)
def test_non_integer_rejected_naming_the_setting(
    field: tuple[str, int, int, str], value: object
) -> None:
    """Any non-integer value fails construction with an error naming the key.

    R1.9: a non-integer configuration value for either bound must be rejected at
    startup. Pydantic surfaces this as a ``ValidationError`` whose location is
    the offending alias, so the key name appears in the error text.
    """
    alias, _low, _high, _attr = field
    with pytest.raises(pydantic.ValidationError) as exc_info:
        _build_settings(alias, value)
    assert alias in str(exc_info.value)
