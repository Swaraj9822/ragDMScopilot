"""Property test for log-stream pass-through of the capture handler.

Feature: ai-observability-platform.

This test exercises the additive contract of
:class:`~rag_system.observability_tracing.log_handler.TracePersistingLogHandler`
(task 13.x): the handler is attached to a logger *next to* the existing
:class:`logging.StreamHandler` configured by
:func:`rag_system.observability.setup_logging`, captures every emitted record
into the durable log buffer, and — crucially for R17.6 — does **not** suppress,
reformat, or otherwise interfere with the existing stream output. Every record
the logger emits must still be written to the existing structured log stream
exactly as it would be without the capture handler attached.

The property compares two configurations driven by the same generated records:

* a *baseline* logger with only the structured ``StreamHandler``;
* a *capture* logger with the same ``StreamHandler`` plus a
  ``TracePersistingLogHandler``.

Pass-through holds iff the captured configuration's stream output is byte-for-byte
identical to the baseline, while the buffer simultaneously receives every record
(confirming the capture is genuinely additive rather than a no-op).
"""

from __future__ import annotations

import io
import json
import logging
import threading
from itertools import count

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import (
    MetricsRegistry,
    _JSONFormatter,
    _TraceContextFilter,
)
from rag_system.observability_tracing.buffers import BoundedLogBuffer
from rag_system.observability_tracing.log_handler import TracePersistingLogHandler

# A process-wide counter guarantees each generated logger gets a unique name so
# examples never share handler state through the logging manager's cache.
_logger_ids = count()
_logger_ids_lock = threading.Lock()

# Log levels the structured stream emits, by numeric value, paired with their
# canonical name (what the StreamHandler renders and the buffer captures).
_LEVELS = st.sampled_from(
    [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    ]
)

# Messages avoid newlines/carriage returns so each emitted record maps to
# exactly one line of stream output, keeping the line-wise comparison exact.
_messages = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="\r\n"),
    min_size=0,
    max_size=80,
)

# A "set of captured log records" (R17.6): each record is a (level, message)
# pair. An empty list is allowed — the vacuous case still must not error.
_record_lists = st.lists(st.tuples(_LEVELS, _messages), min_size=0, max_size=30)


def _unique_logger_name() -> str:
    with _logger_ids_lock:
        return f"rag_system.test.passthrough.{next(_logger_ids)}"


def _build_stream_handler(stream: io.StringIO) -> logging.StreamHandler:
    """Build a StreamHandler mirroring the production structured-stream setup."""
    handler = logging.StreamHandler(stream)
    handler.addFilter(_TraceContextFilter())
    handler.setFormatter(_JSONFormatter())
    handler.setLevel(logging.NOTSET)
    return handler


def _emit_all(logger: logging.Logger, records: list[tuple[int, str]]) -> None:
    for level, message in records:
        logger.log(level, message)


# ---------------------------------------------------------------------------
# Property 33 - captured logs are still emitted to the existing log stream.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 33: Captured logs are still emitted to the existing log stream
# Validates: Requirements 17.6
@settings(max_examples=100)
@given(records=_record_lists)
def test_capture_handler_preserves_existing_stream_output(
    records: list[tuple[int, str]],
) -> None:
    """The capture handler is additive: it never suppresses stream output.

    For any set of emitted records, attaching ``TracePersistingLogHandler``
    alongside the structured ``StreamHandler`` leaves the stream output
    byte-for-byte identical to the baseline (no record dropped, reformatted, or
    swallowed), while the buffer simultaneously captures every record (R17.6).
    """
    # Both phases reuse one logger name so the JSON ``logger`` field is identical;
    # handlers are fully removed between phases, so no state leaks across them.
    logger_name = _unique_logger_name()

    # --- Baseline: StreamHandler only -------------------------------------
    baseline_stream = io.StringIO()
    baseline_logger = logging.getLogger(logger_name)
    baseline_logger.propagate = False
    baseline_logger.setLevel(logging.DEBUG)
    baseline_logger.addHandler(_build_stream_handler(baseline_stream))
    try:
        _emit_all(baseline_logger, records)
    finally:
        for handler in list(baseline_logger.handlers):
            baseline_logger.removeHandler(handler)
            handler.close()
    baseline_output = baseline_stream.getvalue()

    # --- Capture: StreamHandler + TracePersistingLogHandler ---------------
    capture_stream = io.StringIO()
    buffer = BoundedLogBuffer(capacity=10_000, metrics=MetricsRegistry())
    capture_logger = logging.getLogger(logger_name)
    capture_logger.propagate = False
    capture_logger.setLevel(logging.DEBUG)
    stream_handler = _build_stream_handler(capture_stream)
    capture_handler = TracePersistingLogHandler(buffer)
    capture_handler.addFilter(_TraceContextFilter())
    # Order mirrors setup: existing StreamHandler first, capture handler added
    # next to it. Pass-through must hold regardless of relative ordering.
    capture_logger.addHandler(stream_handler)
    capture_logger.addHandler(capture_handler)
    try:
        _emit_all(capture_logger, records)
    finally:
        for handler in list(capture_logger.handlers):
            capture_logger.removeHandler(handler)
            handler.close()
    capture_output = capture_stream.getvalue()

    # Pass-through: the existing stream output is unchanged by the capture
    # handler. Compare line-by-line on every structured field except the
    # second-resolution ``ts`` (which legitimately differs between the two
    # independent emissions and is unrelated to the capture handler).
    baseline_lines = baseline_output.splitlines()
    capture_lines = capture_output.splitlines()
    assert len(capture_lines) == len(baseline_lines)
    for baseline_line, capture_line in zip(baseline_lines, capture_lines):
        baseline_entry = json.loads(baseline_line)
        capture_entry = json.loads(capture_line)
        baseline_entry.pop("ts", None)
        capture_entry.pop("ts", None)
        assert capture_entry == baseline_entry

    # Every emitted record produced exactly one line on the existing stream.
    expected_lines = len(records)
    assert capture_output.count("\n") == expected_lines
    assert len(capture_lines) == expected_lines

    # Additive: the capture handler genuinely captured every record too, so the
    # equality above reflects pass-through rather than the handler being a no-op.
    captured = buffer.drain()
    assert len(captured) == expected_lines
    for (level, message), model in zip(records, captured):
        assert model.level == logging.getLevelName(level)
        assert model.message == message
