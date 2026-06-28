"""Unit tests for RetentionScheduler.

Validates:
- R13.4: Trace_Store retention enforcement cycle runs at configured interval (≤ 24h)
- R18.3: Log_Store retention enforcement cycle runs at configured interval (≤ 24h)
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from rag_system.observability_tracing.retention_scheduler import (
    MAX_INTERVAL_HOURS,
    RetentionScheduler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stores():
    """Create mock trace and log stores with enforce_retention methods."""
    trace_store = MagicMock()
    trace_store.enforce_retention = MagicMock()
    log_store = MagicMock()
    log_store.enforce_retention = MagicMock()
    return trace_store, log_store


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    """Validate constructor argument constraints."""

    def test_rejects_zero_interval(self):
        trace_store, log_store = _make_stores()
        with pytest.raises(ValueError, match="positive"):
            RetentionScheduler(trace_store, log_store, interval_hours=0)

    def test_rejects_negative_interval(self):
        trace_store, log_store = _make_stores()
        with pytest.raises(ValueError, match="positive"):
            RetentionScheduler(trace_store, log_store, interval_hours=-1)

    def test_caps_interval_at_24_hours(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store, log_store, interval_hours=48.0
        )
        assert scheduler._interval_hours == MAX_INTERVAL_HOURS


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Test start/stop/is_running behaviour."""

    def test_not_running_before_start(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store, log_store, interval_hours=1.0
        )
        assert not scheduler.is_running

    def test_start_and_stop(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store, log_store, interval_hours=1.0
        )
        scheduler.start()
        assert scheduler.is_running
        scheduler.stop(timeout=2.0)
        assert not scheduler.is_running

    def test_start_is_idempotent(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store, log_store, interval_hours=1.0
        )
        scheduler.start()
        thread1 = scheduler._thread
        scheduler.start()
        thread2 = scheduler._thread
        assert thread1 is thread2
        scheduler.stop(timeout=2.0)

    def test_daemon_thread(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store, log_store, interval_hours=1.0
        )
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        scheduler.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Enforcement behaviour
# ---------------------------------------------------------------------------


class TestEnforcement:
    """Test that retention enforcement is invoked correctly."""

    def test_invokes_trace_retention(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=72.0,
            log_retention_hours=None,
            interval_hours=0.001,  # very short for test speed
        )
        scheduler.start()
        time.sleep(0.1)  # Give the scheduler time to run at least once
        scheduler.stop(timeout=2.0)
        trace_store.enforce_retention.assert_called_with(timedelta(hours=72.0))
        log_store.enforce_retention.assert_not_called()

    def test_invokes_log_retention(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=None,
            log_retention_hours=168.0,
            interval_hours=0.001,
        )
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop(timeout=2.0)
        trace_store.enforce_retention.assert_not_called()
        log_store.enforce_retention.assert_called_with(timedelta(hours=168.0))

    def test_invokes_both_stores(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=48.0,
            log_retention_hours=96.0,
            interval_hours=0.001,
        )
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop(timeout=2.0)
        trace_store.enforce_retention.assert_called_with(timedelta(hours=48.0))
        log_store.enforce_retention.assert_called_with(timedelta(hours=96.0))

    def test_skips_both_when_no_retention_configured(self):
        trace_store, log_store = _make_stores()
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=None,
            log_retention_hours=None,
            interval_hours=0.001,
        )
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop(timeout=2.0)
        trace_store.enforce_retention.assert_not_called()
        log_store.enforce_retention.assert_not_called()


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------


class TestFailureResilience:
    """Scheduler must not crash on enforcement failure (log WARNING, continue)."""

    def test_trace_store_failure_does_not_crash(self):
        trace_store, log_store = _make_stores()
        trace_store.enforce_retention.side_effect = RuntimeError("DB down")
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=24.0,
            log_retention_hours=48.0,
            interval_hours=0.001,
        )
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop(timeout=2.0)
        # The scheduler should still be responsive (stop worked)
        assert not scheduler.is_running
        # Log store was still called despite trace store failure
        log_store.enforce_retention.assert_called_with(timedelta(hours=48.0))

    def test_log_store_failure_does_not_crash(self):
        trace_store, log_store = _make_stores()
        log_store.enforce_retention.side_effect = RuntimeError("DB down")
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=24.0,
            log_retention_hours=48.0,
            interval_hours=0.001,
        )
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop(timeout=2.0)
        assert not scheduler.is_running
        # Trace store was still called
        trace_store.enforce_retention.assert_called_with(timedelta(hours=24.0))

    def test_both_stores_fail_does_not_crash(self):
        trace_store, log_store = _make_stores()
        trace_store.enforce_retention.side_effect = RuntimeError("DB down")
        log_store.enforce_retention.side_effect = RuntimeError("DB down")
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=24.0,
            log_retention_hours=48.0,
            interval_hours=0.001,
        )
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop(timeout=2.0)
        assert not scheduler.is_running


# ---------------------------------------------------------------------------
# Stop interrupts sleep
# ---------------------------------------------------------------------------


class TestStopInterruptsSleep:
    """stop() should wake the thread from its interruptible sleep immediately."""

    def test_stop_returns_quickly(self):
        trace_store, log_store = _make_stores()
        # Use a large interval so the thread would sleep for a long time
        scheduler = RetentionScheduler(
            trace_store,
            log_store,
            trace_retention_hours=24.0,
            interval_hours=1.0,  # 1 hour — would hang if sleep not interruptible
        )
        scheduler.start()
        time.sleep(0.05)  # Let it enter the sleep
        start = time.monotonic()
        scheduler.stop(timeout=2.0)
        elapsed = time.monotonic() - start
        # Should stop well within 2 seconds (event.wait is interruptible)
        assert elapsed < 2.0
        assert not scheduler.is_running
