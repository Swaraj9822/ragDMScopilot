"""Thread-safe bounded in-memory buffers for spans and log records.

These buffers decouple in-process capture (on the request path) from durable
persistence (on background flush workers). They are deliberately simple ring
buffers with a hard capacity: when full they DROP the newly offered entry
(they never evict already-buffered entries) and increment a dropped counter on
the shared :class:`~rag_system.observability.MetricsRegistry`, never raising to
the caller.

Design references:
- R9.4 / R17.3: buffer up to 10,000 entries while the store is unavailable.
- R9.5 / R17.4: on overflow, discard newly offered entries and increment a
  dropped counter without raising.
- Buffer access is guarded by a lock, consistent with ``MetricsRegistry``'s
  ``RLock`` usage.
"""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Generic, TypeVar

from ..observability import MetricsRegistry
from ..observability import metrics as _default_metrics
from .models import LogRecordModel, Span

__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "BoundedBuffer",
    "BoundedSpanBuffer",
    "BoundedLogBuffer",
]

DEFAULT_BUFFER_CAPACITY = 10_000
"""Maximum number of entries a buffer holds before it starts dropping (R9.4)."""

_T = TypeVar("_T")


class BoundedBuffer(Generic[_T]):
    """A thread-safe, capacity-bounded buffer that drops on overflow.

    The buffer holds at most ``capacity`` entries. When :meth:`add` is called
    while the buffer is already full, the new entry is discarded (existing
    entries are retained) and the configured dropped counter is incremented on
    the metrics registry. :meth:`add` never raises as a result of overflow.

    All access is serialised by an :class:`~threading.RLock`, mirroring the
    locking convention used by :class:`MetricsRegistry`.
    """

    def __init__(
        self,
        dropped_metric: str,
        *,
        capacity: int = DEFAULT_BUFFER_CAPACITY,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._dropped_metric = dropped_metric
        self._metrics = metrics if metrics is not None else _default_metrics
        self._lock = RLock()
        self._items: deque[_T] = deque()

    @property
    def capacity(self) -> int:
        """The maximum number of entries the buffer will retain."""
        return self._capacity

    def add(self, item: _T) -> bool:
        """Offer ``item`` to the buffer.

        Returns ``True`` when the entry was buffered, ``False`` when the buffer
        was at capacity and the entry was dropped. On a drop, the configured
        dropped counter is incremented by exactly one. Never raises on overflow.
        """
        with self._lock:
            if len(self._items) >= self._capacity:
                # Drop the NEW entry; retain the already-buffered ones (R9.5/R17.4).
                self._metrics.increment(self._dropped_metric)
                return False
            self._items.append(item)
            return True

    def drain(self) -> list[_T]:
        """Atomically return all buffered entries and clear the buffer.

        Used by the background flush workers to take ownership of the current
        batch. Returned entries preserve insertion order.
        """
        with self._lock:
            if not self._items:
                return []
            drained = list(self._items)
            self._items.clear()
            return drained

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class BoundedSpanBuffer(BoundedBuffer[Span]):
    """Bounded buffer of :class:`Span` objects awaiting persistence."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_BUFFER_CAPACITY,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        super().__init__(
            "rag_spans_dropped_total",
            capacity=capacity,
            metrics=metrics,
        )


class BoundedLogBuffer(BoundedBuffer[LogRecordModel]):
    """Bounded buffer of :class:`LogRecordModel` objects awaiting persistence."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_BUFFER_CAPACITY,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        super().__init__(
            "rag_logs_dropped_total",
            capacity=capacity,
            metrics=metrics,
        )
