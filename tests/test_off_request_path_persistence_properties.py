"""Property test for off-request-path persistence (Property 20).

Feature: ai-observability-platform.

This module exercises the real request-path capture components together with the
real background flush workers from
:mod:`rag_system.observability_tracing.flush_workers`, asserting the design's
Property 20 / R9.1, R17.1:

* **No synchronous store write on the request path** — running the
  latency-sensitive capture operations (the :class:`SpanRecorder` recording
  spans into the bounded span buffer via ``buffer.add``, and the
  :class:`TracePersistingLogHandler` capturing log records into the bounded log
  buffer) performs **zero** Trace_Store or Log_Store writes. Immediately after
  capture, before any worker has been started, the stores have received nothing.
* **Writes happen only off the request path** — every Trace_Store and Log_Store
  write is performed by the :class:`TraceFlushWorker` / :class:`LogFlushWorker`
  background thread, never on the request (main) thread.

The store doubles are *recording* stores: each :meth:`persist` call records the
identity of the thread that performed it. The request/capture work runs on the
main thread, so the property reduces to two checks — the stores are untouched
while capture runs, and once the workers drain the buffers every recorded write
happened on a thread other than the main (request) thread.
"""

from __future__ import annotations

import logging
import threading
import time
from threading import Lock

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedLogBuffer, BoundedSpanBuffer
from rag_system.observability_tracing.flush_workers import (
    LogFlushWorker,
    TraceFlushWorker,
    group_spans_by_trace,
)
from rag_system.observability_tracing.log_handler import TracePersistingLogHandler
from rag_system.observability_tracing.recorder import SpanRecorder

#: A short cadence so the background flush loop drains the buffers promptly.
_FLUSH_INTERVAL = 0.01
#: Generous upper bound for a buffer to drain on a background thread.
_DRAIN_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _AlwaysSampler:
    """Sampler stub that records every trace (so capture always produces spans)."""

    def should_record(self, *, trace_id: str | None, has_trace_header: bool) -> bool:
        return True


class _RecordingStore:
    """Store double that records the thread identity of every ``persist`` call.

    Used for both the Trace_Store (``TraceFlushWorker`` calls ``persist(trace)``)
    and the Log_Store (``LogFlushWorker`` defaults to ``persist(record)``). The
    recorded thread idents are what the property inspects: capture work runs on
    the main thread, so any write attributed to the main thread would mean a
    synchronous, on-request-path persistence (a Property-20 violation).
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self.write_threads: list[int] = []

    def persist(self, payload: object) -> None:
        with self._lock:
            self.write_threads.append(threading.get_ident())


# ---------------------------------------------------------------------------
# Smart generators - the work a single instrumented request performs.
# ---------------------------------------------------------------------------

#: Child-span operation labels recorded under the request's root span.
_operations = st.lists(st.text(min_size=1, max_size=16), min_size=0, max_size=6)

#: Standard logging levels a request might emit.
_LEVELS = st.sampled_from(
    [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
)


@st.composite
def _log_records(draw: st.DrawFn) -> list[logging.LogRecord]:
    """A non-empty batch of log records a request emits on its path."""
    count = draw(st.integers(min_value=1, max_value=6))
    records: list[logging.LogRecord] = []
    for _ in range(count):
        records.append(
            logging.LogRecord(
                name=draw(st.text(min_size=1, max_size=12)),
                level=draw(_LEVELS),
                pathname="",
                lineno=0,
                msg=draw(st.text(max_size=40)),
                args=(),
                exc_info=None,
            )
        )
    return records


def _wait_until_drained(buffer: object, timeout: float = _DRAIN_TIMEOUT) -> bool:
    """Poll until *buffer* is empty (drained by the background worker)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(buffer) == 0:  # type: ignore[arg-type]
            return True
        time.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# Property 20 - persistence happens only off the request path.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 20: Persistence happens only off the request path
# Validates: Requirements 9.1, 17.1
@settings(max_examples=100, deadline=None)
@given(operations=_operations, log_records=_log_records())
def test_persistence_happens_only_off_the_request_path(
    operations: list[str],
    log_records: list[logging.LogRecord],
) -> None:
    """For any instrumented request, capture performs no synchronous store
    writes; every Trace_Store and Log_Store write is done by a background flush
    worker off the request (main) thread (R9.1, R17.1)."""
    main_ident = threading.get_ident()
    metrics = MetricsRegistry()

    span_buffer = BoundedSpanBuffer(metrics=metrics)
    log_buffer = BoundedLogBuffer(metrics=metrics)
    trace_store = _RecordingStore()
    log_store = _RecordingStore()

    # -- Request path: capture spans + logs entirely on the main thread -----
    recorder = SpanRecorder(
        sampler=_AlwaysSampler(),
        span_buffer=span_buffer,
        metrics=metrics,
    )
    trace_id = "0123456789abcdef0123456789abcdef"
    with recorder.start_trace(trace_id=trace_id, route="/query"):
        for operation in operations:
            with recorder.record_span(operation):
                pass

    handler = TracePersistingLogHandler(log_buffer)
    for record in log_records:
        handler.emit(record)

    # The capture path must have buffered work but written nothing to a store.
    assert len(span_buffer) >= 1, "the root span should have been captured"
    assert len(log_buffer) == len(log_records)
    assert trace_store.write_threads == [], "trace capture wrote to the store synchronously"
    assert log_store.write_threads == [], "log capture wrote to the store synchronously"

    # -- Off the request path: background workers drain the buffers ---------
    # All buffered spans belong to the single request trace, so resolve every
    # span to that trace id for grouping (Span carries no trace_id field).
    trace_worker = TraceFlushWorker(
        span_buffer,
        trace_store,
        grouper=lambda spans: group_spans_by_trace(spans, trace_id_of=lambda _s: trace_id),
        interval=_FLUSH_INTERVAL,
    )
    log_worker = LogFlushWorker(log_buffer, log_store, interval=_FLUSH_INTERVAL)

    trace_worker.start()
    log_worker.start()
    try:
        assert _wait_until_drained(span_buffer), "trace worker did not drain the span buffer"
        assert _wait_until_drained(log_buffer), "log worker did not drain the log buffer"
    finally:
        # drain=False: the buffers are already empty, and a drain-on-stop would
        # run flush_once on THIS (main) thread, which must not perform writes.
        trace_worker.stop(drain=False)
        log_worker.stop(drain=False)

    # -- The writes happened, and every one was off the request thread ------
    assert len(trace_store.write_threads) == 1, "the one request trace should persist once"
    assert len(log_store.write_threads) == len(log_records)

    for ident in trace_store.write_threads + log_store.write_threads:
        assert ident != main_ident, "a store write occurred synchronously on the request thread"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
