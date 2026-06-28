"""Domain models for the AI observability tracing platform.

These are the in-memory execution-trace models plus the stored (serializer
boundary) shapes. They live alongside the existing :mod:`rag_system.models`
types; the application-level ``QueryTraceRecord`` there is intentionally left
untouched. ``Trace`` here is a distinct, lower-level execution trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict

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


@dataclass
class Trace:
    """A full execution trace composed of one or more spans."""

    trace_id: str                         # 32-char lowercase hex
    route: str
    start_ts: datetime                    # UTC
    duration_ms: int                      # non-negative integer
    root_status: SpanStatus
    spans: list[Span] = field(default_factory=list)  # 1..10000


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
