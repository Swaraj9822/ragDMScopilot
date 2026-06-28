"""Trace serialization — the boundary between in-memory :class:`Trace` objects
and their stored (PostgreSQL row) representation.

:class:`TraceSerializer` converts a :class:`Trace` to a :class:`StoredTrace`
dict (``serialize``) and rebuilds a full :class:`Trace` from a well-formed
:class:`StoredTrace` (``deserialize``). Timestamps cross the boundary as
ISO-8601 UTC strings for both the trace and every span.

This module implements task 7.1.

Requirements covered:

* R6.1 — ``serialize`` produces a :class:`StoredTrace` including every span and
  every span attribute, with no span, attribute, or value omitted, added, or
  altered.
* R6.2 — ``deserialize`` rebuilds a :class:`Trace` containing every span and
  span attribute present in a well-formed stored representation.
* R6.4 — a malformed stored representation (unparseable, or missing a required
  field: trace_id, span identifier, span parent reference, duration, or status)
  raises :class:`TraceDeserializationError` carrying the malformation *reason*
  and the affected *trace_id*, and never returns a partially built Trace.
* R6.5 — when neither the affected trace_id nor a specific malformation reason
  can be determined, a generic :class:`TraceDeserializationError` is raised
  indicating that deserialization failed.
* R6.6 — a serialization failure raises :class:`TraceSerializationError` with
  the affected trace_id and writes nothing partial.

Span attribute values are scalar (``str | int | float | bool``) because
coercion happens at capture time (see :class:`SpanRecorder`), so the serializer
preserves attribute maps verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .models import Span, StoredSpan, StoredTrace, Trace

__all__ = [
    "TraceDeserializationError",
    "TraceSerializationError",
    "TraceSerializer",
]

#: Reason recorded when deserialization fails without a determinable cause.
_GENERIC_DESERIALIZE_REASON = "deserialization failed"

#: Permitted terminal status values for a trace root or span.
_VALID_STATUSES = ("success", "error")


class TraceSerializationError(Exception):
    """Raised when a :class:`Trace` cannot be serialized (R6.6).

    Carries the affected *trace_id* (``None`` when it could not be determined)
    so the caller can correlate the failure without any partial representation
    having been written.
    """

    def __init__(self, reason: str, trace_id: str | None) -> None:
        self.reason = reason
        self.trace_id = trace_id
        super().__init__(
            f"failed to serialize trace {trace_id!r}: {reason}"
        )


class TraceDeserializationError(Exception):
    """Raised when a stored trace representation cannot be deserialized (R6.4, R6.5).

    Carries the malformation *reason* and the affected *trace_id*. Either may be
    ``None`` when it could not be determined; when both are absent the error
    represents a generic deserialization failure (R6.5).
    """

    def __init__(self, reason: str | None, trace_id: str | None) -> None:
        self.reason = reason
        self.trace_id = trace_id
        super().__init__(
            f"failed to deserialize trace {trace_id!r}: "
            f"{reason or _GENERIC_DESERIALIZE_REASON}"
        )


def _to_iso_utc(value: Any) -> str:
    """Render a timestamp as an ISO-8601 string normalised to UTC.

    A naive ``datetime`` is assumed to already be UTC; an aware ``datetime`` is
    converted to UTC. Anything that is not a ``datetime`` raises ``TypeError``,
    which the caller surfaces as a serialization failure.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _parse_iso_utc(value: Any) -> datetime:
    """Parse an ISO-8601 UTC string back into an aware UTC ``datetime``.

    Accepts a trailing ``Z`` designator. Raises ``ValueError``/``TypeError`` on
    anything unparseable, which the caller surfaces as a malformation.
    """
    if not isinstance(value, str):
        raise TypeError(f"expected ISO-8601 string, got {type(value).__name__}")
    text = value
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TraceSerializer:
    """Converts a :class:`Trace` to and from its stored representation."""

    # ------------------------------------------------------------------
    # Serialization (Trace -> StoredTrace)
    # ------------------------------------------------------------------

    def serialize(self, trace: Trace) -> StoredTrace:
        """Serialize *trace* into a :class:`StoredTrace` (R6.1, R6.6).

        Every span and every span attribute is included verbatim; timestamps are
        rendered as ISO-8601 UTC strings. If serialization fails for any reason,
        a :class:`TraceSerializationError` is raised carrying the affected
        trace_id and nothing partial is produced.
        """
        trace_id = getattr(trace, "trace_id", None)
        try:
            stored: StoredTrace = {
                "trace_id": trace.trace_id,
                "route": trace.route,
                "start_ts": _to_iso_utc(trace.start_ts),
                "duration_ms": trace.duration_ms,
                "root_status": trace.root_status,
                "spans": [self._serialize_span(span) for span in trace.spans],
            }
            return stored
        except TraceSerializationError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a serialization failure
            raise TraceSerializationError(
                reason=str(exc) or exc.__class__.__name__,
                trace_id=trace_id if isinstance(trace_id, str) else None,
            ) from exc

    def _serialize_span(self, span: Span) -> StoredSpan:
        """Serialize a single :class:`Span` into a :class:`StoredSpan`."""
        stored_span: StoredSpan = {
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "operation": span.operation,
            "start_ts": _to_iso_utc(span.start_ts),
            "duration_ms": span.duration_ms,
            "status": span.status,
            # Copy so the stored representation never aliases the live span's
            # attribute map; values are scalar so a shallow copy is sufficient.
            "attributes": dict(span.attributes),
        }
        return stored_span

    # ------------------------------------------------------------------
    # Deserialization (StoredTrace -> Trace)
    # ------------------------------------------------------------------

    def deserialize(self, stored: StoredTrace) -> Trace:
        """Rebuild a full :class:`Trace` from a well-formed stored representation.

        On malformed input a :class:`TraceDeserializationError` is raised with
        the malformation reason and the affected trace_id where determinable
        (R6.4); when neither can be determined a generic failure is raised
        (R6.5). A partially built Trace is never returned.
        """
        trace_id: str | None = None
        try:
            if not isinstance(stored, Mapping):
                raise TraceDeserializationError(
                    reason="stored trace representation is not a mapping",
                    trace_id=None,
                )

            raw_trace_id = stored.get("trace_id")
            if isinstance(raw_trace_id, str) and raw_trace_id:
                # Capture early so subsequent malformations can be attributed.
                trace_id = raw_trace_id
            else:
                raise TraceDeserializationError(
                    reason="missing required field: trace_id",
                    trace_id=raw_trace_id if isinstance(raw_trace_id, str) else None,
                )

            route = stored.get("route")
            if not isinstance(route, str):
                raise TraceDeserializationError(
                    reason="missing required field: route",
                    trace_id=trace_id,
                )

            start_ts = self._require_timestamp(
                stored.get("start_ts"), trace_id, field="start_ts"
            )
            duration_ms = self._require_duration(
                stored.get("duration_ms"), trace_id, field="duration_ms"
            )
            root_status = self._require_status(
                stored.get("root_status"), trace_id, field="root_status"
            )

            raw_spans = stored.get("spans")
            if not isinstance(raw_spans, list):
                raise TraceDeserializationError(
                    reason="missing required field: spans",
                    trace_id=trace_id,
                )

            spans = [self._deserialize_span(raw, trace_id) for raw in raw_spans]

            return Trace(
                trace_id=trace_id,
                route=route,
                start_ts=start_ts,
                duration_ms=duration_ms,
                root_status=root_status,
                spans=spans,
            )
        except TraceDeserializationError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a deserialization failure
            # Unexpected failure: report a specific reason when we have one and a
            # known trace_id, otherwise a generic failure (R6.5).
            if trace_id is None:
                raise TraceDeserializationError(
                    reason=None, trace_id=None
                ) from exc
            raise TraceDeserializationError(
                reason=str(exc) or exc.__class__.__name__,
                trace_id=trace_id,
            ) from exc

    def _deserialize_span(self, raw: Any, trace_id: str) -> Span:
        """Rebuild a single :class:`Span` from its stored mapping.

        Validates the required span fields (span identifier, parent reference,
        duration, status) and surfaces any malformation as a
        :class:`TraceDeserializationError` carrying *trace_id*.
        """
        if not isinstance(raw, Mapping):
            raise TraceDeserializationError(
                reason="span entry is not a mapping",
                trace_id=trace_id,
            )

        raw_span_id = raw.get("span_id")
        if not isinstance(raw_span_id, str) or not raw_span_id:
            raise TraceDeserializationError(
                reason="missing required field: span_id",
                trace_id=trace_id,
            )
        span_id = raw_span_id

        # The parent reference must be present, but null (None) is valid and
        # denotes the Root_Span (R5.3, R7.5).
        if "parent_span_id" not in raw:
            raise TraceDeserializationError(
                reason=f"missing required field: parent_span_id (span {span_id})",
                trace_id=trace_id,
            )
        parent_span_id = raw["parent_span_id"]
        if parent_span_id is not None and not isinstance(parent_span_id, str):
            raise TraceDeserializationError(
                reason=f"invalid parent_span_id (span {span_id})",
                trace_id=trace_id,
            )

        operation = raw.get("operation")
        if not isinstance(operation, str):
            raise TraceDeserializationError(
                reason=f"missing required field: operation (span {span_id})",
                trace_id=trace_id,
            )

        start_ts = self._require_timestamp(
            raw.get("start_ts"), trace_id, field=f"start_ts (span {span_id})"
        )
        duration_ms = self._require_duration(
            raw.get("duration_ms"), trace_id, field=f"duration_ms (span {span_id})"
        )
        status = self._require_status(
            raw.get("status"), trace_id, field=f"status (span {span_id})"
        )

        attributes = raw.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise TraceDeserializationError(
                reason=f"invalid attributes (span {span_id})",
                trace_id=trace_id,
            )

        return Span(
            span_id=span_id,
            parent_span_id=parent_span_id,
            operation=operation,
            start_ts=start_ts,
            duration_ms=duration_ms,
            status=status,
            attributes=dict(attributes),
        )

    # ------------------------------------------------------------------
    # Field validators (raise TraceDeserializationError on malformation)
    # ------------------------------------------------------------------

    @staticmethod
    def _require_timestamp(value: Any, trace_id: str, *, field: str) -> datetime:
        if value is None:
            raise TraceDeserializationError(
                reason=f"missing required field: {field}",
                trace_id=trace_id,
            )
        try:
            return _parse_iso_utc(value)
        except (ValueError, TypeError) as exc:
            raise TraceDeserializationError(
                reason=f"unparseable {field}: {exc}",
                trace_id=trace_id,
            ) from exc

    @staticmethod
    def _require_duration(value: Any, trace_id: str, *, field: str) -> int:
        # bool is a subclass of int but is not a valid duration.
        if value is None:
            raise TraceDeserializationError(
                reason=f"missing required field: {field}",
                trace_id=trace_id,
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TraceDeserializationError(
                reason=f"invalid {field}: expected a non-negative integer",
                trace_id=trace_id,
            )
        return value

    @staticmethod
    def _require_status(value: Any, trace_id: str, *, field: str) -> Any:
        if value is None:
            raise TraceDeserializationError(
                reason=f"missing required field: {field}",
                trace_id=trace_id,
            )
        if value not in _VALID_STATUSES:
            raise TraceDeserializationError(
                reason=f"invalid {field}: expected one of {_VALID_STATUSES}",
                trace_id=trace_id,
            )
        return value
