"""AI observability tracing platform.

Request-tracing and log-persistence built additively on top of the existing
RAG system. This package exposes the in-memory execution-trace domain models
and their stored (serializer boundary) shapes.
"""

from __future__ import annotations

from functools import lru_cache

from rag_system.observability_tracing.context import (
    bind_span,
    get_active_span_id,
    get_active_trace_id,
    propagate_into_thread,
    restore_span,
)
from rag_system.observability_tracing.flush_workers import (
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    MAX_DRAIN_LATENCY_SECONDS,
    LogFlushWorker,
    TraceFlushWorker,
    group_spans_by_trace,
)
from rag_system.observability_tracing.log_handler import (
    TracePersistingLogHandler,
)
from rag_system.observability_tracing.log_serializer import (
    LogDeserializationError,
    LogSerializer,
    StoredLog,
)
from rag_system.observability_tracing.models import (
    AttributeValue,
    LogRecordModel,
    Span,
    SpanStatus,
    StoredSpan,
    StoredTrace,
    Trace,
)
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.serializer import (
    TraceDeserializationError,
    TraceSerializationError,
    TraceSerializer,
)

__all__ = [
    "AttributeValue",
    "DEFAULT_FLUSH_INTERVAL_SECONDS",
    "LogDeserializationError",
    "LogFlushWorker",
    "LogRecordModel",
    "LogSerializer",
    "MAX_DRAIN_LATENCY_SECONDS",
    "Span",
    "SpanRecorder",
    "SpanStatus",
    "StoredLog",
    "StoredSpan",
    "StoredTrace",
    "Trace",
    "TraceDeserializationError",
    "TraceFlushWorker",
    "TraceSerializationError",
    "TraceSerializer",
    "TracePersistingLogHandler",
    "bind_span",
    "get_active_span_id",
    "get_active_trace_id",
    "get_span_recorder",
    "group_spans_by_trace",
    "propagate_into_thread",
    "restore_span",
]


@lru_cache
def get_span_recorder() -> SpanRecorder:
    """Return the module-level SpanRecorder singleton.

    Constructs the recorder on first call using the application settings. The
    recorder is wired with a :class:`TraceSampler` and a
    :class:`BoundedSpanBuffer` so spans are captured in-process and flushed to
    the store by background workers.
    """
    from rag_system.config import get_settings
    from rag_system.observability import metrics as app_metrics

    from .buffers import BoundedSpanBuffer
    from .sampler import TraceSampler

    settings = get_settings()
    sampler = TraceSampler(
        enabled=settings.tracing_enabled,
        sample_rate=settings.trace_sample_rate,
    )
    span_buffer = BoundedSpanBuffer(metrics=app_metrics)
    return SpanRecorder(
        sampler=sampler,
        span_buffer=span_buffer,
        metrics=app_metrics,
    )
