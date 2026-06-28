"""Background flush workers that drain in-memory buffers into the durable stores.

This module implements task 12.1: :class:`TraceFlushWorker` and
:class:`LogFlushWorker`. Each is a daemon-thread batch loop that runs *off* the
request-response path, draining a bounded in-memory buffer and writing its
contents to the corresponding PostgreSQL store. They decouple the latency-
sensitive capture layer (the :class:`~rag_system.observability_tracing.recorder.SpanRecorder`
and the log-capture handler) from persistence, so a slow or unavailable store
never blocks or fails a live request.

Design references
-----------------
* R9.1 / R17.1 — *all* store writes happen here, on a background thread; the
  request path performs no synchronous trace/log store writes.
* R9.6 / R17.5 — when the store becomes available again after a period of
  unavailability, every buffered entry is written within 30 seconds. The flush
  loop polls at :data:`DEFAULT_FLUSH_INTERVAL_SECONDS` (well under the 30 second
  bound) and a single :meth:`flush_once` drains the *entire* buffer, so the
  whole backlog is written on the first cycle after recovery.
* The workers are daemon threads with a small batch loop, mirroring the existing
  ``trace-writer`` daemon-thread pattern in
  :meth:`rag_system.service.RagService._persist_query_trace_async`.

Store-availability contract
---------------------------
A flush worker cannot persist while the store is unavailable, so it must not
drop entries it failed to write (that would violate R9.6 / R17.5). The workers
therefore interpret an exception raised by the store's ``persist`` call as a
transient "store unavailable" signal: the affected entries are returned to the
buffer and retried on the next cycle (so a backlog drains within one interval of
recovery). A clean return from ``persist`` is treated as a completed write — the
store is itself responsible for best-effort discard of individually malformed
entries (R5.4 / R14.5). Re-buffering uses the buffer's normal :meth:`add`, so if
the buffer is at capacity the overflow is dropped and counted exactly as any
other overflow (R9.5 / R17.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Thread
from typing import Any, Callable, Generic, Iterable, Sequence, TypeVar

from .models import LogRecordModel, Span, Trace

__all__ = [
    "DEFAULT_FLUSH_INTERVAL_SECONDS",
    "MAX_DRAIN_LATENCY_SECONDS",
    "TraceFlushWorker",
    "LogFlushWorker",
    "group_spans_by_trace",
]

#: How often the flush loop wakes to drain its buffer. Chosen comfortably below
#: :data:`MAX_DRAIN_LATENCY_SECONDS` so that, after the store recovers, the next
#: poll drains the entire backlog inside the 30 second budget (R9.6, R17.5).
DEFAULT_FLUSH_INTERVAL_SECONDS = 1.0

#: The hard upper bound (seconds) within which buffered entries must be written
#: after store recovery (R9.6, R17.5). The configured interval must not exceed
#: this value.
MAX_DRAIN_LATENCY_SECONDS = 30.0

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Current time as a timezone-aware UTC timestamp (matches the domain models)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Span -> Trace grouping (R9.1: "spans grouped by trace")
# ---------------------------------------------------------------------------


def group_spans_by_trace(
    spans: Iterable[Span],
    *,
    trace_id_of: Callable[[Span], str | None] = lambda span: getattr(span, "trace_id", None),
    route_of: Callable[[Span], str | None] = lambda span: getattr(span, "route", None),
) -> list[Trace]:
    """Group buffered *spans* into :class:`Trace` objects, one per trace id.

    Spans are grouped by their trace id (resolved via *trace_id_of*). Within a
    group the Root_Span (the span whose ``parent_span_id`` is ``None``) supplies
    the trace-level fields — route, start timestamp, duration, and root status;
    when a group has no identifiable root those fields fall back to the earliest
    span's values. Spans whose trace id cannot be resolved are skipped (they
    cannot be attached to any trace and are not persistable on their own).

    The grouping is deterministic and order-preserving: traces appear in the
    order their first span was drained, and each trace's spans keep their drained
    order, so the worker's behaviour is reproducible.
    """
    groups: dict[str, list[Span]] = {}
    order: list[str] = []
    for span in spans:
        trace_id = trace_id_of(span)
        if not isinstance(trace_id, str) or not trace_id:
            # Cannot be attributed to a trace; nothing persistable to do.
            continue
        if trace_id not in groups:
            groups[trace_id] = []
            order.append(trace_id)
        groups[trace_id].append(span)

    traces: list[Trace] = []
    for trace_id in order:
        group = groups[trace_id]
        root = next((s for s in group if s.parent_span_id is None), None)
        anchor = root if root is not None else min(group, key=lambda s: s.start_ts)
        route = route_of(anchor) or anchor.operation
        traces.append(
            Trace(
                trace_id=trace_id,
                route=str(route),
                start_ts=anchor.start_ts,
                duration_ms=anchor.duration_ms,
                root_status=anchor.status,
                spans=list(group),
            )
        )
    return traces


# ---------------------------------------------------------------------------
# Base flush worker (daemon-thread batch loop)
# ---------------------------------------------------------------------------


@dataclass
class _FlushBatch(Generic[_T]):
    """A unit of persistence and its source buffer entries.

    ``payload`` is what the worker hands to the store (a :class:`Trace` for the
    trace worker, a :class:`LogRecordModel` for the log worker). ``sources`` are
    the original buffer entries the payload was assembled from; when persistence
    fails they are returned to the buffer for retry, so re-buffering happens at
    the same granularity the buffer holds (R9.6 / R17.5).
    """

    payload: Any
    sources: list[_T] = field(default_factory=list)


class _BaseFlushWorker(Generic[_T]):
    """Daemon-thread batch loop that drains a buffer into a store.

    Subclasses provide :meth:`_plan` (turn a drained batch into a list of
    :class:`_FlushBatch`) and :meth:`_persist_payload` (write one payload). The
    base class owns the thread lifecycle, the poll cadence, and the
    retain-on-failure behaviour.
    """

    def __init__(
        self,
        name: str,
        buffer: Any,
        *,
        interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        logger_: logging.Logger | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be a positive number of seconds")
        if interval > MAX_DRAIN_LATENCY_SECONDS:
            # The drain-within-30s guarantee (R9.6/R17.5) requires the loop to
            # wake at least that often.
            raise ValueError(
                f"interval must not exceed {MAX_DRAIN_LATENCY_SECONDS} seconds "
                "to honour the post-recovery drain guarantee"
            )
        self._name = name
        self._buffer = buffer
        self._interval = interval
        self._logger = logger_ or logger
        self._stop_event = Event()
        self._thread: Thread | None = None

    # -- thread lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Start the background daemon thread (idempotent).

        A daemon thread is used so the worker never keeps the process alive,
        matching the existing ``trace-writer`` pattern.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None, drain: bool = True) -> None:
        """Signal the loop to stop and wait for the thread to finish.

        When *drain* is true a final :meth:`flush_once` is attempted after the
        thread has exited so entries captured right before shutdown are not
        silently lost.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None
        if drain:
            try:
                self.flush_once()
            except Exception:  # noqa: BLE001 - shutdown flush is best-effort
                self._logger.warning(
                    "Final %s flush on shutdown failed", self._name, exc_info=True
                )

    @property
    def is_running(self) -> bool:
        """Whether the background thread is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        """The batch loop: drain, persist, sleep — until asked to stop."""
        while not self._stop_event.is_set():
            try:
                self.flush_once()
            except Exception:  # noqa: BLE001 - a flush failure must not kill the loop
                self._logger.warning(
                    "%s flush cycle failed; will retry", self._name, exc_info=True
                )
            # Interruptible sleep: wake immediately when stop() is called.
            self._stop_event.wait(self._interval)

    # -- one batch ----------------------------------------------------------

    def flush_once(self) -> int:
        """Drain the buffer once and persist its contents off the request path.

        Returns the number of payloads successfully persisted. Entries that could
        not be written (because the store raised, i.e. is unavailable) are
        returned to the buffer for the next cycle, so a backlog is drained within
        one interval of the store recovering (R9.6, R17.5).
        """
        drained: Sequence[_T] = self._buffer.drain()
        if not drained:
            return 0

        persisted = 0
        retained: list[_T] = []
        for batch in self._plan(list(drained)):
            try:
                self._persist_payload(batch.payload)
                persisted += 1
            except Exception:  # noqa: BLE001 - store unavailable: retain & retry
                self._logger.warning(
                    "%s failed to persist a batch; retaining for retry",
                    self._name,
                    exc_info=True,
                )
                retained.extend(batch.sources)

        # Re-buffer retained sources via the buffer's normal add(), so overflow
        # is dropped and counted exactly like any other overflow (R9.5/R17.4).
        for source in retained:
            self._buffer.add(source)

        return persisted

    # -- subclass hooks -----------------------------------------------------

    def _plan(self, drained: list[_T]) -> list[_FlushBatch[_T]]:
        raise NotImplementedError

    def _persist_payload(self, payload: Any) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Trace flush worker
# ---------------------------------------------------------------------------


class TraceFlushWorker(_BaseFlushWorker[Any]):
    """Drains the span buffer into the trace store, grouping spans by trace.

    Each flush groups the drained spans into :class:`Trace` objects (R9.1's
    "spans grouped by trace") and persists each trace through the store's
    ``persist`` method, which writes the trace and all of its spans in a single
    atomic transaction. When the buffer already holds assembled :class:`Trace`
    objects (the wiring layer may enqueue completed traces directly) they are
    persisted as-is. Persistence happens entirely on this background thread, so
    the request path performs no synchronous trace-store writes (R9.1, R17.1).

    Args:
        span_buffer: The bounded buffer drained each cycle. Its ``drain()``
            returns the buffered entries (spans and/or traces) and clears it; its
            ``add()`` re-buffers entries that could not be written.
        trace_store: An object exposing ``persist(trace: Trace) -> None``
            (e.g. :class:`~rag_system.observability_tracing.trace_store.PostgresTraceStore`
            or the in-memory store double). A raised exception is treated as the
            store being unavailable and triggers retain-and-retry.
        grouper: Converts a drained batch of spans into a list of traces;
            defaults to :func:`group_spans_by_trace`.
        interval: Poll cadence in seconds (default
            :data:`DEFAULT_FLUSH_INTERVAL_SECONDS`, capped at
            :data:`MAX_DRAIN_LATENCY_SECONDS`).
        logger_: Optional logger; defaults to this module's logger.
    """

    def __init__(
        self,
        span_buffer: Any,
        trace_store: Any,
        *,
        grouper: Callable[[Iterable[Span]], list[Trace]] = group_spans_by_trace,
        interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        logger_: logging.Logger | None = None,
    ) -> None:
        super().__init__("trace-flush-worker", span_buffer, interval=interval, logger_=logger_)
        self._store = trace_store
        self._grouper = grouper

    def _plan(self, drained: list[Any]) -> list[_FlushBatch[Any]]:
        """Group drained spans into traces, preserving each trace's source spans.

        Items that are already :class:`Trace` objects are persisted directly
        (their source is the trace itself); the remaining :class:`Span` items are
        grouped by trace id. Re-buffering on failure restores the original
        entries, so spans are re-buffered as spans and traces as traces.
        """
        traces: list[Trace] = [item for item in drained if isinstance(item, Trace)]
        spans: list[Span] = [item for item in drained if not isinstance(item, Trace)]

        batches: list[_FlushBatch[Any]] = [
            _FlushBatch(payload=trace, sources=[trace]) for trace in traces
        ]
        if spans:
            for trace in self._grouper(spans):
                # The trace's sources are exactly the spans that composed it.
                batches.append(_FlushBatch(payload=trace, sources=list(trace.spans)))
        return batches

    def _persist_payload(self, payload: Any) -> None:
        self._store.persist(payload)


# ---------------------------------------------------------------------------
# Log flush worker
# ---------------------------------------------------------------------------


class LogFlushWorker(_BaseFlushWorker[LogRecordModel]):
    """Drains the log buffer into the log store, one record per write.

    Each flush drains the buffered :class:`LogRecordModel` entries and persists
    each through the store's ``persist`` method. All writes happen on this
    background thread, so the request path performs no synchronous log-store
    writes (R17.1). A record the store could not write (because it raised, i.e.
    is unavailable) is returned to the buffer and retried on the next cycle, so a
    backlog drains within 30 seconds of the store recovering (R17.5).

    Args:
        log_buffer: The bounded buffer drained each cycle.
        log_store: An object that persists a single :class:`LogRecordModel`.
            The persistence call defaults to ``log_store.persist`` (the
            :class:`~rag_system.observability_tracing.log_store.PostgresLogStore`
            interface); pass *persist* to target a differently named method such
            as the in-memory store double's ``persist_log``. A raised exception
            is treated as the store being unavailable and triggers
            retain-and-retry.
        persist: Optional explicit ``persist(record) -> None`` callable; defaults
            to ``log_store.persist``.
        interval: Poll cadence in seconds (default
            :data:`DEFAULT_FLUSH_INTERVAL_SECONDS`, capped at
            :data:`MAX_DRAIN_LATENCY_SECONDS`).
        logger_: Optional logger; defaults to this module's logger.
    """

    def __init__(
        self,
        log_buffer: Any,
        log_store: Any,
        *,
        persist: Callable[[LogRecordModel], None] | None = None,
        interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        logger_: logging.Logger | None = None,
    ) -> None:
        super().__init__("log-flush-worker", log_buffer, interval=interval, logger_=logger_)
        self._store = log_store
        self._persist = persist if persist is not None else log_store.persist

    def _plan(self, drained: list[LogRecordModel]) -> list[_FlushBatch[LogRecordModel]]:
        """One payload per record; its source is the record itself."""
        return [_FlushBatch(payload=record, sources=[record]) for record in drained]

    def _persist_payload(self, payload: LogRecordModel) -> None:
        self._persist(payload)
