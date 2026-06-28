"""Periodic retention enforcement scheduler for the AI observability platform.

:class:`RetentionScheduler` is a daemon-thread scheduler that periodically
invokes :meth:`~rag_system.observability_tracing.trace_store.PostgresTraceStore.enforce_retention`
and :meth:`~rag_system.observability_tracing.log_store.PostgresLogStore.enforce_retention`
to remove entries that have exceeded the configured retention period.

Design references
-----------------
* R13.4 — WHERE a retention period is configured, the Trace_Store SHALL execute
  a retention enforcement cycle at a configured interval not exceeding 24 hours.
* R18.3 — the same guarantee applies to the Log_Store.

The scheduler follows the same daemon-thread pattern used by
:class:`~rag_system.observability_tracing.flush_workers.TraceFlushWorker` and
:class:`~rag_system.observability_tracing.flush_workers.LogFlushWorker`:

* ``threading.Thread(daemon=True)`` so the scheduler never keeps the process
  alive.
* ``threading.Event()`` for stop signaling and interruptible sleep so
  :meth:`stop` can wake the thread immediately.
* On enforcement failure, log at WARNING and continue — the scheduler thread
  must never crash.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from threading import Event, Thread
from typing import Any

__all__ = [
    "MAX_INTERVAL_HOURS",
    "RetentionScheduler",
]

#: The maximum interval (hours) between retention enforcement cycles (R13.4,
#: R18.3). Any configured interval is capped to this value.
MAX_INTERVAL_HOURS: float = 24.0

logger = logging.getLogger(__name__)


class RetentionScheduler:
    """Daemon-thread scheduler that runs retention enforcement periodically.

    Args:
        trace_store: An object exposing
            ``enforce_retention(max_age: timedelta | None) -> None`` (e.g.
            :class:`~rag_system.observability_tracing.trace_store.PostgresTraceStore`).
        log_store: An object exposing
            ``enforce_retention(max_age: timedelta | None) -> None`` (e.g.
            :class:`~rag_system.observability_tracing.log_store.PostgresLogStore`).
        trace_retention_hours: The maximum age (in hours) for traces. Traces
            strictly older than this are deleted. ``None`` means no retention
            period is configured — trace retention enforcement is skipped.
        log_retention_hours: The maximum age (in hours) for log records.
            ``None`` means no retention period is configured — log retention
            enforcement is skipped.
        interval_hours: How often (in hours) the scheduler wakes to run a
            retention cycle. Capped at :data:`MAX_INTERVAL_HOURS` (24 hours).
        logger_: Optional logger; defaults to this module's logger.
    """

    def __init__(
        self,
        trace_store: Any,
        log_store: Any,
        *,
        trace_retention_hours: float | None = None,
        log_retention_hours: float | None = None,
        interval_hours: float = 24.0,
        logger_: logging.Logger | None = None,
    ) -> None:
        if interval_hours <= 0:
            raise ValueError("interval_hours must be a positive number")
        # Cap at maximum allowed interval (R13.4, R18.3).
        self._interval_hours = min(interval_hours, MAX_INTERVAL_HOURS)
        self._trace_store = trace_store
        self._log_store = log_store
        self._trace_retention_hours = trace_retention_hours
        self._log_retention_hours = log_retention_hours
        self._logger = logger_ or logger
        self._stop_event = Event()
        self._thread: Thread | None = None

    # -- public interface ---------------------------------------------------

    def start(self) -> None:
        """Start the background daemon thread (idempotent).

        The thread runs at the configured interval, invoking retention
        enforcement on both stores each cycle. It is a daemon thread so it
        never keeps the process alive.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(
            target=self._run, name="retention-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Signal the scheduler to stop and wait for the thread to finish.

        Args:
            timeout: Maximum seconds to wait for the thread to join. ``None``
                means wait indefinitely.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    @property
    def is_running(self) -> bool:
        """Whether the scheduler thread is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    # -- internal -----------------------------------------------------------

    def _run(self) -> None:
        """The scheduler loop: enforce retention, then sleep until next cycle."""
        interval_seconds = self._interval_hours * 3600.0
        while not self._stop_event.is_set():
            self._enforce_cycle()
            # Interruptible sleep: wake immediately when stop() is called.
            self._stop_event.wait(interval_seconds)

    def _enforce_cycle(self) -> None:
        """Run one retention enforcement cycle for both stores.

        Each store is invoked independently; a failure in one does not prevent
        the other from running. On any exception, log at WARNING and continue
        (the scheduler thread must never crash).
        """
        # Trace retention
        if self._trace_retention_hours is not None:
            try:
                max_age = timedelta(hours=self._trace_retention_hours)
                self._trace_store.enforce_retention(max_age)
            except Exception as exc:  # noqa: BLE001 - must not crash
                try:
                    self._logger.warning(
                        "Retention enforcement failed for trace store: %s",
                        exc,
                    )
                except Exception:  # noqa: BLE001 - a failing warning must not propagate
                    pass

        # Log retention
        if self._log_retention_hours is not None:
            try:
                max_age = timedelta(hours=self._log_retention_hours)
                self._log_store.enforce_retention(max_age)
            except Exception as exc:  # noqa: BLE001 - must not crash
                try:
                    self._logger.warning(
                        "Retention enforcement failed for log store: %s",
                        exc,
                    )
                except Exception:  # noqa: BLE001 - a failing warning must not propagate
                    pass
