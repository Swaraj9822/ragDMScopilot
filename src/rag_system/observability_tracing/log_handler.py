"""Log capture — the logging handler that feeds the durable log store.

:class:`TracePersistingLogHandler` is a :class:`logging.Handler` attached to the
root logger *next to* the existing :class:`logging.StreamHandler` configured by
:func:`rag_system.observability.setup_logging`. On every emitted record it builds
a :class:`~rag_system.observability_tracing.models.LogRecordModel` and enqueues it
into a bounded in-memory buffer for off-path persistence. It is purely additive:
it does not format, mutate, or suppress the record, so the existing StreamHandler
still emits its single-line JSON object exactly as before.

This module implements task 13.1.

Requirements covered:

* R14.1 — every field present in the emitted structured log record is captured
  into the :class:`LogRecordModel` (UTC timestamp, level, logger name, message,
  trace_id correlation, exception text, and the stage-specific ``extra`` fields),
  ready for the log store to persist.
* R17.6 — capture is non-blocking and best-effort: the model is offered to a
  bounded buffer that drops on overflow rather than blocking, the handler never
  raises into the logging call site, and because the handler is independent of
  the StreamHandler the existing log stream still receives the record.

Design notes
------------
* The handler does no I/O. It only constructs a model and calls
  :meth:`~rag_system.observability_tracing.buffers.BoundedLogBuffer.add`, which is
  itself non-blocking (it drops the newest entry and counts the drop when full).
* ``extra`` values are coerced to scalars at capture time, mirroring
  :meth:`SpanRecorder.set_attributes`, so the buffer and serializer only ever see
  ``str | int | float | bool`` values (consistent with R3.7).
* Each captured record is stamped with a monotonically increasing
  ``insertion_seq`` so log records sharing a timestamp retain a stable ordering
  tiebreaker (R15.2) even before the database assigns a row id.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone

from ..observability import _EXTRA_FIELDS, get_trace_id
from .buffers import BoundedLogBuffer
from .models import AttributeValue, LogRecordModel

__all__ = ["TracePersistingLogHandler"]

#: Placeholder the trace-context log filter writes when no trace is active; it
#: must be normalised back to an explicit ``None`` on the captured model (R14.3).
_NULL_TRACE_SENTINEL = "-"

#: Scalar attribute types permitted on a captured ``extra`` map; anything else is
#: stringified, mirroring :meth:`SpanRecorder.set_attributes` (R3.7).
_SCALAR_TYPES = (str, bool, int, float)

#: Fields captured from the log record as the dedicated correlation column rather
#: than as part of the free-form ``extra`` map.
_CORRELATION_FIELDS = frozenset({"trace_id"})


class TracePersistingLogHandler(logging.Handler):
    """A logging handler that captures records into the durable log buffer.

    The handler is attached alongside the existing StreamHandler. On each
    :meth:`emit` it builds a :class:`LogRecordModel` from the standard
    :class:`logging.LogRecord` and enqueues it without blocking. It never formats
    or writes the record to any stream itself, so the StreamHandler's JSON line is
    emitted unchanged (R17.6).

    Args:
        log_buffer: The bounded buffer into which captured records are enqueued.
            Its :meth:`add` is non-blocking and drops (counting the drop) when
            full, so the handler imposes no back-pressure on the logging call
            site.
        level: The minimum level this handler captures; defaults to
            :data:`logging.NOTSET` so it captures every record the root logger
            passes to it.
    """

    def __init__(
        self,
        log_buffer: BoundedLogBuffer,
        *,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self._buffer = log_buffer
        # Monotonic per-handler sequence; ``next`` on an itertools.count is atomic
        # under CPython's GIL, providing a lock-free ordering tiebreaker (R15.2).
        self._seq = itertools.count()

    # ------------------------------------------------------------------
    # Handler API
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Capture *record* into the log buffer (non-blocking, best-effort).

        Builds a :class:`LogRecordModel` retaining every field of the emitted
        record and offers it to the bounded buffer. Any failure is routed through
        :meth:`logging.Handler.handleError` rather than propagating, so a capture
        problem never disrupts the logging call site or the sibling StreamHandler
        (R17.6).
        """
        try:
            model = self._build_model(record)
            # Non-blocking: the buffer drops (and counts) on overflow (R17.6).
            self._buffer.add(model)
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            self.handleError(record)

    # ------------------------------------------------------------------
    # Model construction
    # ------------------------------------------------------------------

    def _build_model(self, record: logging.LogRecord) -> LogRecordModel:
        """Build a :class:`LogRecordModel` capturing every field of *record* (R14.1)."""
        return LogRecordModel(
            timestamp=self._timestamp(record),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
            trace_id=self._trace_id(record),
            exc_text=self._exc_text(record),
            extra=self._extra(record),
            insertion_seq=next(self._seq),
        )

    @staticmethod
    def _timestamp(record: logging.LogRecord) -> datetime:
        """Return the record's creation time as a timezone-aware UTC datetime (R14.2)."""
        return datetime.fromtimestamp(record.created, tz=timezone.utc)

    @staticmethod
    def _trace_id(record: logging.LogRecord) -> str | None:
        """Resolve the correlation trace_id for *record*, or ``None`` (R14.3).

        Prefers the ``trace_id`` attribute set by the trace-context log filter,
        normalising its null placeholder back to ``None``; falls back to the
        active trace id on the context when the attribute is absent.
        """
        raw = getattr(record, "trace_id", None)
        if raw is None or raw == _NULL_TRACE_SENTINEL:
            raw = get_trace_id()
        if raw is None or raw == _NULL_TRACE_SENTINEL:
            return None
        return raw if isinstance(raw, str) else str(raw)

    def _exc_text(self, record: logging.LogRecord) -> str | None:
        """Format the record's exception traceback, or ``None`` when absent.

        Mirrors the ``exc`` field produced by the structured JSON formatter so a
        persisted record carries the same exception text it emits to the stream.
        """
        if record.exc_info and record.exc_info[0] is not None:
            return self._formatter().formatException(record.exc_info)
        return None

    @staticmethod
    def _extra(record: logging.LogRecord) -> dict[str, AttributeValue]:
        """Capture the stage-specific ``extra`` fields present on *record* (R14.1).

        Iterates the same allow-list of fields the structured JSON formatter
        emits, excluding the dedicated ``trace_id`` correlation column and the
        null placeholder, and coerces every value to a scalar (R3.7).
        """
        extra: dict[str, AttributeValue] = {}
        for key in _EXTRA_FIELDS:
            if key in _CORRELATION_FIELDS:
                continue
            value = getattr(record, key, None)
            if value is None:
                continue
            extra[key] = value if isinstance(value, _SCALAR_TYPES) else str(value)
        return extra

    @staticmethod
    def _formatter() -> logging.Formatter:
        """Return a bare formatter used only for exception text rendering."""
        return logging.Formatter()
