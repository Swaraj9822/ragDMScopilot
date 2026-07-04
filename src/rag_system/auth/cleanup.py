"""Background cleanup of expired refresh tokens.

The auth flow never removes rows from ``refresh_tokens``: an expired token is
rejected on refresh but left in place, so without a periodic sweep the table
grows roughly one row per login/refresh forever. :class:`RefreshTokenCleanupScheduler`
is a daemon-thread scheduler that periodically deletes rows whose ``expires_at``
has passed.

It mirrors the daemon-thread pattern used by the observability
:class:`~rag_system.observability_tracing.retention_scheduler.RetentionScheduler`:

* ``threading.Thread(daemon=True)`` so it never keeps the process alive;
* ``threading.Event`` for stop signaling and interruptible sleep;
* best-effort error handling — a failed sweep is logged and the thread keeps
  running (a transient DB outage must not kill the cleaner).
"""

from __future__ import annotations

from threading import Event, Thread
from typing import Any

from rag_system.observability import get_logger

logger = get_logger(__name__)

__all__ = ["RefreshTokenCleanupScheduler"]

#: Hard cap on the cleanup interval; a token table does not need pruning more
#: coarsely than daily, and this bounds the worst-case row buildup between runs.
MAX_INTERVAL_HOURS: float = 24.0


class RefreshTokenCleanupScheduler:
    """Daemon-thread scheduler that periodically prunes expired refresh tokens.

    Args:
        store: An object exposing ``delete_expired() -> int`` (e.g.
            :class:`~rag_system.auth.refresh_store.PostgresRefreshTokenStore`).
        interval_hours: How often (in hours) to run a sweep. Capped at
            :data:`MAX_INTERVAL_HOURS`.
    """

    def __init__(self, store: Any, *, interval_hours: float = MAX_INTERVAL_HOURS) -> None:
        if interval_hours <= 0:
            raise ValueError("interval_hours must be a positive number")
        self._store = store
        self._interval_seconds = min(interval_hours, MAX_INTERVAL_HOURS) * 3600.0
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """Start the background daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(
            target=self._run, name="refresh-token-cleanup", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Signal the scheduler to stop and wait for the thread to finish."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        # Defer the first sweep by one interval: expired tokens are already
        # rejected on use, so there is no urgency to prune at boot, and
        # deferring avoids a startup database hit. ``Event.wait`` returns True
        # once stop() is signalled, so the loop exits promptly on shutdown.
        while not self._stop_event.wait(self._interval_seconds):
            self._sweep()

    def _sweep(self) -> None:
        try:
            self._store.delete_expired()
        except Exception:  # noqa: BLE001 - a failed sweep must not crash the daemon
            logger.warning("Refresh token cleanup sweep failed", exc_info=True)
