"""Unit tests for the refresh-token cleanup scheduler.

Verifies the daemon-thread scheduler prunes expired refresh tokens periodically
and never crashes on a failing sweep.
"""

from __future__ import annotations

import time

import pytest

from rag_system.auth.cleanup import MAX_INTERVAL_HOURS, RefreshTokenCleanupScheduler


class _RecordingStore:
    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls = 0
        self._raise = raise_on_call

    def delete_expired(self) -> int:
        self.calls += 1
        if self._raise:
            raise RuntimeError("DB down")
        return 3


def test_rejects_non_positive_interval():
    with pytest.raises(ValueError, match="positive"):
        RefreshTokenCleanupScheduler(_RecordingStore(), interval_hours=0)


def test_interval_is_capped_at_max():
    scheduler = RefreshTokenCleanupScheduler(
        _RecordingStore(), interval_hours=48.0
    )
    assert scheduler._interval_seconds == MAX_INTERVAL_HOURS * 3600.0


def test_sweep_calls_store_delete_expired():
    store = _RecordingStore()
    scheduler = RefreshTokenCleanupScheduler(store, interval_hours=1.0)
    scheduler._sweep()
    assert store.calls == 1


def test_sweep_swallows_store_errors():
    store = _RecordingStore(raise_on_call=True)
    scheduler = RefreshTokenCleanupScheduler(store, interval_hours=1.0)
    # A failing sweep must not propagate (the daemon must keep running).
    scheduler._sweep()
    assert store.calls == 1


def test_start_and_stop():
    store = _RecordingStore()
    scheduler = RefreshTokenCleanupScheduler(store, interval_hours=1.0)
    scheduler.start()
    try:
        assert scheduler.is_running is True
    finally:
        scheduler.stop(timeout=2.0)
    assert scheduler.is_running is False


def test_start_is_idempotent():
    store = _RecordingStore()
    scheduler = RefreshTokenCleanupScheduler(store, interval_hours=1.0)
    scheduler.start()
    try:
        first = scheduler._thread
        scheduler.start()  # no-op while already running
        assert scheduler._thread is first
    finally:
        scheduler.stop(timeout=2.0)


def test_stop_wakes_the_thread_before_the_interval_elapses():
    # The scheduler sleeps a full interval between sweeps; stop() must wake it
    # immediately rather than block for the (long) interval.
    store = _RecordingStore()
    scheduler = RefreshTokenCleanupScheduler(store, interval_hours=24.0)
    scheduler.start()
    started = time.monotonic()
    scheduler.stop(timeout=2.0)
    assert scheduler.is_running is False
    assert time.monotonic() - started < 2.0
