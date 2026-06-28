"""Log serialization — the boundary between in-memory :class:`LogRecordModel`
objects and their stored (PostgreSQL row) representation.

:class:`LogSerializer` converts a :class:`LogRecordModel` to a stored dict
(``serialize``) and rebuilds a full :class:`LogRecordModel` from that dict
(``deserialize``). The pair is a faithful identity round-trip: every field
present on the model survives ``deserialize(serialize(record))`` unchanged
(see Property 18).

This module implements task 7.2.

Requirements covered:

* R14.1 — ``serialize`` retains every field present in the emitted log record
  (timestamp, level, logger, message, trace_id, exc_text, extra), and the
  insertion-order tiebreaker (``insertion_seq``) so the round-trip is faithful.
* R14.2 — the record timestamp is stored in UTC as an ISO-8601 string and
  restored as a timezone-aware UTC ``datetime``.
* R14.3 — a null or absent ``trace_id`` is stored as an explicit ``None`` and
  restored as ``None``.

``extra`` values are scalar (``str | int | float | bool``) because coercion
happens at capture time, so the serializer preserves the ``extra`` map verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from .models import AttributeValue, LogRecordModel
from .serializer import _parse_iso_utc, _to_iso_utc

__all__ = [
    "LogDeserializationError",
    "LogSerializer",
    "StoredLog",
]


class StoredLog(TypedDict):
    """Dict shape of a log record as written to / read from a PostgreSQL row.

    The ``timestamp`` crosses the boundary as an ISO-8601 UTC string; ``trace_id``
    and ``exc_text`` are explicitly nullable; ``insertion_seq`` is retained so the
    stored shape round-trips faithfully (the database assigns the row ``id`` from
    this insertion order — R15.2).
    """

    timestamp: str                       # ISO-8601 UTC
    level: str
    logger: str
    message: str
    trace_id: str | None                 # explicit null when absent (R14.3)
    exc_text: str | None
    extra: dict[str, AttributeValue]
    insertion_seq: int


class LogDeserializationError(Exception):
    """Raised when a stored log representation cannot be deserialized.

    Carries the malformation *reason* and the affected *trace_id* (``None`` when
    it could not be determined). A partially built record is never returned.
    """

    def __init__(self, reason: str, trace_id: str | None = None) -> None:
        self.reason = reason
        self.trace_id = trace_id
        super().__init__(f"failed to deserialize log record: {reason}")


class LogSerializer:
    """Converts a :class:`LogRecordModel` to and from its stored representation."""

    # ------------------------------------------------------------------
    # Serialization (LogRecordModel -> StoredLog)
    # ------------------------------------------------------------------

    def serialize(self, record: LogRecordModel) -> StoredLog:
        """Serialize *record* into a :class:`StoredLog` dict (R14.1, R14.2, R14.3).

        The timestamp is rendered as an ISO-8601 UTC string; an absent
        ``trace_id`` is stored as an explicit ``None``; ``extra`` is copied so the
        stored representation never aliases the live record's map. ``insertion_seq``
        is retained so the round-trip is faithful.
        """
        stored: StoredLog = {
            "timestamp": _to_iso_utc(record.timestamp),
            "level": record.level,
            "logger": record.logger,
            "message": record.message,
            # Explicit null when absent (R14.3).
            "trace_id": record.trace_id,
            "exc_text": record.exc_text,
            # Copy so the stored map never aliases the live record's; values are
            # scalar so a shallow copy is sufficient.
            "extra": dict(record.extra),
            "insertion_seq": record.insertion_seq,
        }
        return stored

    # ------------------------------------------------------------------
    # Deserialization (StoredLog -> LogRecordModel)
    # ------------------------------------------------------------------

    def deserialize(self, stored: StoredLog | Mapping[str, object]) -> LogRecordModel:
        """Rebuild a full :class:`LogRecordModel` from a stored representation.

        The timestamp is parsed back into a timezone-aware UTC ``datetime``
        (R14.2); a stored null ``trace_id`` is restored as ``None`` (R14.3);
        ``extra`` defaults to an empty map and ``insertion_seq`` to ``0`` when
        absent. Malformed input raises :class:`LogDeserializationError` and never
        returns a partially built record.
        """
        if not isinstance(stored, Mapping):
            raise LogDeserializationError(
                "stored log representation is not a mapping"
            )

        raw_trace_id = stored.get("trace_id")
        if raw_trace_id is not None and not isinstance(raw_trace_id, str):
            raise LogDeserializationError("invalid trace_id: expected a string or null")
        trace_id = raw_trace_id

        level = self._require_str(stored, "level", trace_id)
        logger = self._require_str(stored, "logger", trace_id)
        message = self._require_str(stored, "message", trace_id)

        raw_exc_text = stored.get("exc_text")
        if raw_exc_text is not None and not isinstance(raw_exc_text, str):
            raise LogDeserializationError(
                "invalid exc_text: expected a string or null", trace_id
            )
        exc_text = raw_exc_text

        if "timestamp" not in stored:
            raise LogDeserializationError("missing required field: timestamp", trace_id)
        try:
            timestamp = _parse_iso_utc(stored["timestamp"])
        except (ValueError, TypeError) as exc:
            raise LogDeserializationError(
                f"unparseable timestamp: {exc}", trace_id
            ) from exc

        raw_extra = stored.get("extra", {})
        if not isinstance(raw_extra, Mapping):
            raise LogDeserializationError("invalid extra: expected a mapping", trace_id)
        extra: dict[str, AttributeValue] = dict(raw_extra)

        raw_seq = stored.get("insertion_seq", 0)
        if isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
            raise LogDeserializationError(
                "invalid insertion_seq: expected an integer", trace_id
            )
        insertion_seq = raw_seq

        return LogRecordModel(
            timestamp=timestamp,
            level=level,
            logger=logger,
            message=message,
            trace_id=trace_id,
            exc_text=exc_text,
            extra=extra,
            insertion_seq=insertion_seq,
        )

    # ------------------------------------------------------------------
    # Field validators
    # ------------------------------------------------------------------

    @staticmethod
    def _require_str(
        stored: Mapping[str, object], field: str, trace_id: str | None
    ) -> str:
        value = stored.get(field)
        if not isinstance(value, str):
            raise LogDeserializationError(
                f"missing required field: {field}", trace_id
            )
        return value
