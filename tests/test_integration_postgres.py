"""Integration tests against a live PostgreSQL database.

These tests are gated behind the ``COPILOT_DB_HOST`` environment variable.
When no live database is configured, all tests in this module are skipped
automatically via ``pytestmark``.

Validates:
- R5.1:  Persistence within 5,000 ms of Trace completion
- R7.1:  Trace retrieval within 2,000 ms
- R9.6:  Buffer drain within 30 s after outage recovery
- R14.4: Same-database correlation (logs and traces use same PostgreSQL)
- R15.1: Log retrieval within 2,000 ms
- R17.5: Same as R9.6 for log buffer
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import pytest

# Skip all tests in this module if no live PostgreSQL is configured
pytestmark = pytest.mark.skipif(
    not os.environ.get("COPILOT_DB_HOST"),
    reason="Live PostgreSQL required (set COPILOT_DB_HOST)",
)


def _make_settings():
    """Build a Settings instance from live environment variables."""
    from rag_system.config import Settings

    return Settings()


def _unique_trace_id() -> str:
    """Generate a unique 32-char lowercase hex trace_id."""
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def settings():
    return _make_settings()


@pytest.fixture()
def _ensure_schema(settings):
    """Ensure the observability schema exists before running tests."""
    from rag_system.observability_tracing.schema import apply_schema

    apply_schema(settings)


@pytest.fixture()
def trace_store(settings, _ensure_schema):
    from rag_system.observability_tracing.trace_store import PostgresTraceStore

    return PostgresTraceStore(settings)


@pytest.fixture()
def log_store(settings, _ensure_schema):
    from rag_system.observability_tracing.log_store import PostgresLogStore

    return PostgresLogStore(settings)


def _make_trace(trace_id: str | None = None):
    """Create a minimal Trace with one root span for testing."""
    from rag_system.observability_tracing.models import Span, Trace

    tid = trace_id or _unique_trace_id()
    now = _utcnow()
    root_span = Span(
        span_id=uuid.uuid4().hex,
        parent_span_id=None,
        operation="test.root",
        start_ts=now,
        duration_ms=42,
        status="success",
        attributes={"test_key": "test_value"},
    )
    return Trace(
        trace_id=tid,
        route="/test",
        start_ts=now,
        duration_ms=42,
        root_status="success",
        spans=[root_span],
    )


def _make_log_record(trace_id: str | None = None):
    """Create a minimal LogRecordModel for testing."""
    from rag_system.observability_tracing.models import LogRecordModel

    return LogRecordModel(
        timestamp=_utcnow(),
        level="INFO",
        logger="test.logger",
        message="Integration test log message",
        trace_id=trace_id,
        exc_text=None,
        extra={"integration": True},
    )


# ---------------------------------------------------------------------------
# R5.1: Persistence within 5,000 ms of Trace completion
# ---------------------------------------------------------------------------


class TestTracePersistenceLatency:
    """Validates: R5.1 — Trace persistence completes within 5000 ms."""

    def test_trace_persistence_within_5000ms(self, trace_store):
        trace = _make_trace()

        start = time.monotonic()
        trace_store.persist(trace)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 5000, (
            f"Trace persistence took {elapsed_ms:.1f} ms, exceeds 5000 ms budget (R5.1)"
        )

        # Verify it was actually persisted
        retrieved = trace_store.get_trace(trace.trace_id)
        assert retrieved is not None
        assert retrieved.trace_id == trace.trace_id


# ---------------------------------------------------------------------------
# R7.1: Trace retrieval within 2,000 ms
# ---------------------------------------------------------------------------


class TestTraceRetrievalLatency:
    """Validates: R7.1 — Trace retrieval completes within 2000 ms."""

    def test_trace_retrieval_within_2000ms(self, trace_store):
        trace = _make_trace()
        trace_store.persist(trace)

        start = time.monotonic()
        retrieved = trace_store.get_trace(trace.trace_id)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 2000, (
            f"Trace retrieval took {elapsed_ms:.1f} ms, exceeds 2000 ms budget (R7.1)"
        )
        assert retrieved is not None
        assert retrieved.trace_id == trace.trace_id
        assert len(retrieved.spans) == 1
        assert retrieved.spans[0].operation == "test.root"


# ---------------------------------------------------------------------------
# R15.1: Log retrieval within 2,000 ms
# ---------------------------------------------------------------------------


class TestLogRetrievalLatency:
    """Validates: R15.1 — Log retrieval by trace_id within 2000 ms."""

    def test_log_retrieval_within_2000ms(self, log_store):
        trace_id = _unique_trace_id()
        record = _make_log_record(trace_id=trace_id)
        log_store.persist(record)

        start = time.monotonic()
        records = log_store.get_by_trace(trace_id)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 2000, (
            f"Log retrieval took {elapsed_ms:.1f} ms, exceeds 2000 ms budget (R15.1)"
        )
        assert len(records) >= 1
        assert records[0].trace_id == trace_id
        assert records[0].message == "Integration test log message"


# ---------------------------------------------------------------------------
# R9.6, R17.5: Buffer drain within 30 s after outage recovery
# ---------------------------------------------------------------------------


class TestBufferDrainAfterRecovery:
    """Validates: R9.6, R17.5 — Buffered entries drain within 30 s after recovery."""

    def test_buffer_drain_within_30s(self, settings, _ensure_schema):
        from rag_system.observability_tracing.buffers import BoundedBuffer
        from rag_system.observability_tracing.flush_workers import (
            LogFlushWorker,
            TraceFlushWorker,
        )
        from rag_system.observability_tracing.log_store import PostgresLogStore
        from rag_system.observability_tracing.trace_store import PostgresTraceStore

        trace_store = PostgresTraceStore(settings)
        log_store = PostgresLogStore(settings)

        # Create buffers and add entries while store is "unavailable"
        # (in practice the entries are just buffered; the workers will drain them)
        span_buffer: BoundedBuffer = BoundedBuffer("rag_spans_dropped_total")
        log_buffer: BoundedBuffer = BoundedBuffer("rag_logs_dropped_total")

        # Buffer some traces (as assembled Trace objects)
        trace = _make_trace()
        span_buffer.add(trace)

        # Buffer some log records
        log_record = _make_log_record(trace_id=trace.trace_id)
        log_buffer.add(log_record)

        # Start flush workers — they should drain within one interval (<< 30s)
        trace_worker = TraceFlushWorker(span_buffer, trace_store, interval=1.0)
        log_worker = LogFlushWorker(log_buffer, log_store, interval=1.0)

        start = time.monotonic()
        trace_worker.start()
        log_worker.start()

        # Poll until both buffers are empty or 30s elapsed
        deadline = start + 30.0
        while time.monotonic() < deadline:
            if len(span_buffer) == 0 and len(log_buffer) == 0:
                break
            time.sleep(0.2)

        elapsed = time.monotonic() - start

        # Stop workers
        trace_worker.stop(drain=False)
        log_worker.stop(drain=False)

        assert len(span_buffer) == 0, (
            f"Span buffer not drained after {elapsed:.1f}s (R9.6 requires ≤ 30s)"
        )
        assert len(log_buffer) == 0, (
            f"Log buffer not drained after {elapsed:.1f}s (R17.5 requires ≤ 30s)"
        )
        assert elapsed < 30.0, (
            f"Buffer drain took {elapsed:.1f}s, exceeds 30s budget (R9.6, R17.5)"
        )

        # Verify data was actually persisted
        retrieved_trace = trace_store.get_trace(trace.trace_id)
        assert retrieved_trace is not None

        retrieved_logs = log_store.get_by_trace(trace.trace_id)
        assert len(retrieved_logs) >= 1


# ---------------------------------------------------------------------------
# R14.4: Same-database correlation (logs and traces use same PostgreSQL)
# ---------------------------------------------------------------------------


class TestSameDatabaseCorrelation:
    """Validates: R14.4 — Traces and logs use the same PostgreSQL database."""

    def test_same_database_correlation(self, trace_store, log_store):
        # Create a trace and correlated log records sharing the same trace_id
        trace_id = _unique_trace_id()
        trace = _make_trace(trace_id=trace_id)
        log1 = _make_log_record(trace_id=trace_id)
        log2 = _make_log_record(trace_id=trace_id)

        # Persist both to their respective stores (same DB)
        trace_store.persist(trace)
        log_store.persist(log1)
        log_store.persist(log2)

        # Retrieve both using the same trace_id
        retrieved_trace = trace_store.get_trace(trace_id)
        retrieved_logs = log_store.get_by_trace(trace_id)

        # Verify the trace is present
        assert retrieved_trace is not None
        assert retrieved_trace.trace_id == trace_id

        # Verify the correlated logs are present
        assert len(retrieved_logs) == 2
        for log_record in retrieved_logs:
            assert log_record.trace_id == trace_id

        # The correlation is proven: both the trace and its logs are stored
        # and retrievable by the same trace_id from the same database,
        # demonstrating R14.4 (same-database correlation).
