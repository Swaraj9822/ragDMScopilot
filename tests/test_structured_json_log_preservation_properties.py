"""Property test for structured JSON log line preservation.

Feature: ai-observability-platform.

This module exercises the existing structured JSON log formatter
(:class:`rag_system.observability._JSONFormatter`) together with the trace
correlation filter (:class:`rag_system.observability._TraceContextFilter`) to
verify design **Property 26 / R11.1**: for any log record carrying arbitrary
extra fields, the emitted log output is a *single-line* JSON object that

* retains every field present before the tracing layer was added - the
  ``ts``, ``level``, ``logger`` and ``msg`` core fields plus every
  stage-specific ``extra`` field the formatter is configured to emit; and
* includes the ``trace_id`` correlation field set to the active trace
  identifier on the context.

The generators are constrained to the formatter's realistic input domain: a
standard logging level, an arbitrary logger name and message, a 32-char hex
active trace id, and a map of stage-specific extra fields (drawn from the
formatter's allow-list, excluding the dedicated ``trace_id`` correlation key)
with scalar ``str``/``int``/``float``/``bool`` values whose JSON encoding is
exact (NaN/inf excluded).
"""

from __future__ import annotations

import json
import logging

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import (
    _EXTRA_FIELDS,
    _JSONFormatter,
    _TraceContextFilter,
    reset_trace_id,
    set_trace_id,
)

# ---------------------------------------------------------------------------
# Smart generators - constrained to the formatter's valid input domain.
# ---------------------------------------------------------------------------

# Standard Python logging levels; the formatter records ``record.levelname``.
_levels = st.sampled_from(
    [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
)

_loggers = st.text(min_size=1, max_size=40)

_messages = st.text(max_size=200)

# A realistic 32-char lowercase-hex active trace id (never the null sentinel).
_trace_ids = st.text(alphabet="abcdef0123456789", min_size=32, max_size=32)

# Stage-specific extra fields the formatter is configured to emit, excluding the
# dedicated ``trace_id`` correlation key (asserted separately).
_extra_keys = st.sampled_from([k for k in _EXTRA_FIELDS if k != "trace_id"])

# Scalar extra values: str | int | float | bool, excluding NaN/inf so the JSON
# encoding is exact and the round-trip comparison is faithful. bool is generated
# explicitly even though it is a subclass of int.
_extra_values = st.one_of(
    st.text(max_size=40),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.booleans(),
)

_extra = st.dictionaries(keys=_extra_keys, values=_extra_values, max_size=6)


def _make_record(
    logger_name: str, level: int, message: str, extra: dict[str, object]
) -> logging.LogRecord:
    """Build a :class:`logging.LogRecord` carrying *extra* as attributes.

    ``args`` is left empty so ``getMessage`` returns the message verbatim (no
    ``%`` interpolation), letting the message contain arbitrary text.
    """
    fields: dict[str, object] = {
        "name": logger_name,
        "levelname": logging.getLevelName(level),
        "levelno": level,
        "msg": message,
        "args": (),
        **extra,
    }
    return logging.makeLogRecord(fields)


# ---------------------------------------------------------------------------
# Property 26 - structured JSON log line preserves all fields plus correlation.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 26: Structured JSON log line preserves all fields plus trace correlation
# Validates: Requirements 11.1
@settings(max_examples=100)
@given(
    logger_name=_loggers,
    level=_levels,
    message=_messages,
    extra=_extra,
    trace_id=_trace_ids,
)
def test_structured_json_log_preserves_all_fields_plus_trace_correlation(
    logger_name: str,
    level: int,
    message: str,
    extra: dict[str, object],
    trace_id: str,
) -> None:
    """The emitted JSON line retains every pre-tracing field plus ``trace_id`` (R11.1).

    With an active trace identifier on the context, the record is passed through
    the trace-correlation filter and the existing ``_JSONFormatter``. The result
    must be a single-line JSON object that preserves ``ts``/``level``/``logger``/
    ``msg`` and every set ``extra`` field, and carries ``trace_id`` equal to the
    active trace identifier.
    """
    record = _make_record(logger_name, level, message, extra)

    token = set_trace_id(trace_id)
    try:
        # The trace-context filter stamps the active trace id onto the record,
        # exactly as it does in the live logging pipeline.
        _TraceContextFilter().filter(record)
        line = _JSONFormatter().format(record)
    finally:
        reset_trace_id(token)

    # The output is a single physical line: a JSON object with no embedded
    # newline (string values escape control characters).
    assert "\n" not in line
    entry = json.loads(line)
    assert isinstance(entry, dict)

    # Core fields present before the tracing layer are all retained.
    assert "ts" in entry
    assert entry["level"] == logging.getLevelName(level)
    assert entry["logger"] == logger_name
    assert entry["msg"] == message

    # The trace_id correlation field is set to the active trace identifier.
    assert entry["trace_id"] == trace_id

    # Every stage-specific extra field that was set is retained by type and value.
    for key, value in extra.items():
        assert key in entry
        restored = entry[key]
        assert type(restored) is type(value)
        assert restored == value
