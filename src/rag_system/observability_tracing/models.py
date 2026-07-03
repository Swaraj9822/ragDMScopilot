"""Domain models for the AI observability tracing platform.

These are the in-memory execution-trace models plus the stored (serializer
boundary) shapes. They live alongside the existing :mod:`rag_system.models`
types; the application-level ``QueryTraceRecord`` there is intentionally left
untouched. ``Trace`` here is a distinct, lower-level execution trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, NotRequired, TypedDict

# ---------------------------------------------------------------------------
# Scalar aliases
# ---------------------------------------------------------------------------

SpanStatus = Literal["success", "error"]
"""Terminal status of a span or trace root."""

AttributeValue = str | int | float | bool
"""Scalar attribute value. Non-scalar values are coerced at capture time (R3.7)."""


# ---------------------------------------------------------------------------
# In-memory domain model
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """A single timed operation within a trace."""

    span_id: str                          # unique within trace (R1.3)
    parent_span_id: str | None            # None for root (R5.3, R7.5)
    operation: str                        # R1.7
    start_ts: datetime                    # UTC (R5.3)
    duration_ms: int                      # non-negative integer (R1.4, R4.3)
    status: SpanStatus
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    # Identity of the owning trace, stamped by the recorder so the off-path
    # flush worker can group buffered spans by trace without external context
    # (see group_spans_by_trace). Not part of the stored span row (the trace_id
    # lives on the trace), so the serializer ignores it.
    trace_id: str | None = None
    # Route label, set only on the Root_Span so the flush worker can derive the
    # trace's route from its root. Ignored by the span serializer.
    route: str | None = None


@dataclass
class Trace:
    """A full execution trace composed of one or more spans."""

    trace_id: str                         # 32-char lowercase hex
    route: str
    start_ts: datetime                    # UTC
    duration_ms: int                      # non-negative integer
    root_status: SpanStatus
    spans: list[Span] = field(default_factory=list)  # 1..10000
    # Identifier of the AI_Configuration_Version that produced this trace
    # (R9.1). None indicates the configuration was unresolved when the trace was
    # recorded (R9.2); all other trace data is retained regardless.
    ai_configuration_version_id: str | None = None
    # Redacted resolved settings recorded alongside the version id (R9.1, R9.11).
    # Empty dict when the configuration could not be resolved (R9.2).
    resolved_settings: dict[str, object] = field(default_factory=dict)


@dataclass
class LogRecordModel:
    """A captured log record correlated to a trace when one is active."""

    timestamp: datetime                   # UTC (R14.2)
    level: str                            # DEBUG..CRITICAL
    logger: str
    message: str
    trace_id: str | None                  # explicit None allowed (R14.3)
    exc_text: str | None
    extra: dict[str, AttributeValue] = field(default_factory=dict)
    insertion_seq: int = 0                # tiebreaker for ordering (R15.2)


# ---------------------------------------------------------------------------
# Stored representation (serializer boundary)
# ---------------------------------------------------------------------------


class StoredSpan(TypedDict):
    """Dict shape of a span as written to / read from a PostgreSQL row."""

    span_id: str
    parent_span_id: str | None
    operation: str
    start_ts: str                         # ISO-8601 UTC
    duration_ms: int
    status: str
    attributes: dict[str, AttributeValue]


class StoredTrace(TypedDict):
    """Dict shape of a trace as written to / read from PostgreSQL rows."""

    trace_id: str
    route: str
    start_ts: str                         # ISO-8601 UTC
    duration_ms: int
    root_status: str
    spans: list[StoredSpan]
    # Additive/optional: None => unresolved (R9.1, R9.2). NotRequired so existing
    # serializer output (which omits it) remains valid.
    ai_configuration_version_id: NotRequired[str | None]
    # Redacted resolved settings (R9.1, R9.11). NotRequired for backward compat.
    resolved_settings: NotRequired[dict[str, object]]
