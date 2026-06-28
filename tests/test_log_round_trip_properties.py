"""Property test for log serialization round-trip identity.

Feature: ai-observability-platform.

This module exercises :class:`rag_system.observability_tracing.log_serializer.LogSerializer`
across arbitrary valid :class:`LogRecordModel` objects, asserting that

    deserialize(serialize(record))

reproduces a record that preserves *every* field present in the emitted
Log_Record (design Property 18 / R14.1, R14.2): identical timestamp (as a UTC
instant), level, logger name, message, trace_id, exception text, the complete
``extra`` map, and the ``insertion_seq`` tiebreaker.

Generators are constrained to the serializer's valid input domain:

- timezone-aware UTC timestamps (``serialize`` renders ISO-8601 UTC and
  ``deserialize`` returns aware UTC datetimes, so the instant round-trips even
  though tzinfo representation is normalised);
- ``level`` drawn from the standard Python logging level names;
- ``trace_id`` both absent (``None``) and present (a realistic 32-char hex);
- ``exc_text`` both absent (``None``) and present;
- scalar ``extra`` values spanning ``str``/``int``/``float``/``bool`` (NaN
  floats excluded so equality is exact and the round-trip is faithful);
- arbitrary non-negative ``insertion_seq``.

Timestamps are compared as UTC *instants* via :func:`_same_instant`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.log_serializer import LogSerializer
from rag_system.observability_tracing.models import LogRecordModel

# ---------------------------------------------------------------------------
# Smart generators - constrained to the serializer's valid input domain.
# ---------------------------------------------------------------------------

# Standard Python logging level names (R14.2 records the Log_Level verbatim).
_levels = st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])

_loggers = st.text(min_size=1, max_size=40)

_messages = st.text(max_size=200)

# trace_id is either absent (explicit None - R14.3) or a realistic 32-char
# lowercase hex correlation value (present case).
_trace_ids = st.one_of(
    st.none(),
    st.text(alphabet="abcdef0123456789", min_size=32, max_size=32),
)

# Exception text is optional: absent (None) or an arbitrary string.
_exc_texts = st.one_of(st.none(), st.text(max_size=200))

# Scalar extra values: str | int | float | bool, excluding NaN/inf so equality
# is exact and the round-trip is faithful. bool is generated explicitly (it is a
# subclass of int but a distinct attribute value here).
_extra_values = st.one_of(
    st.text(max_size=40),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.booleans(),
)

_extra_keys = st.text(min_size=1, max_size=20)

_extra = st.dictionaries(keys=_extra_keys, values=_extra_values, max_size=6)

# Timezone-aware UTC timestamps; tzinfo is pinned to UTC so generated instants
# are unambiguous and the round-trip is exact.
_utc_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(timezone.utc),
)

# Arbitrary non-negative insertion sequence (the ordering tiebreaker).
_insertion_seqs = st.integers(min_value=0, max_value=10**12)


@st.composite
def _log_records(draw: st.DrawFn) -> LogRecordModel:
    """Build an arbitrary valid :class:`LogRecordModel`.

    Covers trace_id absent/present, exc_text absent/present, varied scalar
    ``extra`` values, UTC timestamps, and arbitrary ``insertion_seq``.
    """
    return LogRecordModel(
        timestamp=draw(_utc_datetimes),
        level=draw(_levels),
        logger=draw(_loggers),
        message=draw(_messages),
        trace_id=draw(_trace_ids),
        exc_text=draw(_exc_texts),
        extra=draw(_extra),
        insertion_seq=draw(_insertion_seqs),
    )


def _same_instant(a: datetime, b: datetime) -> bool:
    """True when two datetimes denote the same UTC instant."""
    return a.astimezone(timezone.utc) == b.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Property 18 - log serialization round-trip preserves all fields.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 18: Log serialization round-trip preserves all fields
# Validates: Requirements 14.1, 14.2
@settings(max_examples=100)
@given(record=_log_records())
def test_log_serialization_round_trip_preserves_all_fields(
    record: LogRecordModel,
) -> None:
    """``deserialize(serialize(record))`` preserves every field (R14.1, R14.2).

    Every field present in the emitted Log_Record survives the round-trip:
    timestamp (compared as a UTC instant), level, logger, message, trace_id
    (including the explicit ``None`` case), exc_text, the complete ``extra``
    key-value map (preserved by type and value), and ``insertion_seq``.
    """
    serializer = LogSerializer()

    restored = serializer.deserialize(serializer.serialize(record))

    # Scalar fields are preserved verbatim.
    assert restored.level == record.level
    assert restored.logger == record.logger
    assert restored.message == record.message
    assert restored.trace_id == record.trace_id
    assert restored.exc_text == record.exc_text
    assert restored.insertion_seq == record.insertion_seq

    # Timestamp is preserved as a UTC instant.
    assert _same_instant(restored.timestamp, record.timestamp)

    # The complete extra map is preserved verbatim - no key omitted, added, or
    # altered, with each value's type and value intact.
    assert set(restored.extra) == set(record.extra)
    for key, value in record.extra.items():
        restored_value = restored.extra[key]
        assert type(restored_value) is type(value)
        if isinstance(value, float):
            assert not math.isnan(restored_value)
        assert restored_value == value
