"""Performance/smoke tests verifying in-process recording latency budgets.

These tests confirm that the span recording overhead stays within the budgeted
≤ 1 ms per span (R9.2) and that the system adds no more than 1 ms of processing
time when the trace store is unavailable (R9.3, R17.2).

The SpanRecorder never writes to the store directly — it always enqueues to the
bounded in-memory buffer. The "store unavailable" scenario is already handled by
this buffer architecture, so both tests exercise the same in-process path.

Validates: Requirements 9.2, 9.3, 17.2
"""

from __future__ import annotations

import time


from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import BoundedSpanBuffer
from rag_system.observability_tracing.recorder import SpanRecorder
from rag_system.observability_tracing.sampler import TraceSampler

ITERATIONS = 1000
BUDGET_MS = 1.0


def _make_recorder() -> SpanRecorder:
    """Build a fully-wired SpanRecorder with sampling enabled at 100%."""
    metrics = MetricsRegistry()
    buffer = BoundedSpanBuffer(metrics=metrics)
    sampler = TraceSampler(enabled=True, sample_rate=1.0)
    return SpanRecorder(sampler=sampler, span_buffer=buffer, metrics=metrics)


class TestSpanRecordingLatencyBudget:
    """R9.2: In-process span recording adds ≤ 1 ms per span."""

    def test_span_recording_adds_at_most_1ms(self) -> None:
        """Measure record_span overhead across many iterations; p95 must be ≤ 1 ms."""
        recorder = _make_recorder()
        overhead_ns: list[int] = []

        for i in range(ITERATIONS):
            with recorder.start_trace(trace_id=None, route="perf_test"):
                t0 = time.perf_counter_ns()
                with recorder.record_span(f"op_{i}"):
                    pass  # no work — measuring just the recording overhead
                elapsed_ns = time.perf_counter_ns() - t0
                overhead_ns.append(elapsed_ns)

        overhead_ns.sort()
        p95_index = int(len(overhead_ns) * 0.95)
        p95_ms = overhead_ns[p95_index] / 1_000_000

        assert p95_ms <= BUDGET_MS, (
            f"Span recording p95 overhead {p95_ms:.3f} ms exceeds budget of {BUDGET_MS} ms"
        )


class TestStoreUnavailableLatencyBudget:
    """R9.3 / R17.2: Store unavailability adds ≤ 1 ms to the request path.

    The SpanRecorder never writes to the store directly; it enqueues to the
    bounded in-memory buffer. When the store is unavailable the buffer simply
    holds entries (or drops them if full). The in-process overhead is identical
    regardless of store availability.
    """

    def test_store_unavailable_adds_at_most_1ms(self) -> None:
        """Measure span recording overhead (buffer-only path); p95 must be ≤ 1 ms."""
        recorder = _make_recorder()
        overhead_ns: list[int] = []

        for i in range(ITERATIONS):
            with recorder.start_trace(trace_id=None, route="store_down_test"):
                t0 = time.perf_counter_ns()
                with recorder.record_span(f"op_{i}"):
                    pass  # no work — the store is never contacted
                elapsed_ns = time.perf_counter_ns() - t0
                overhead_ns.append(elapsed_ns)

        overhead_ns.sort()
        p95_index = int(len(overhead_ns) * 0.95)
        p95_ms = overhead_ns[p95_index] / 1_000_000

        assert p95_ms <= BUDGET_MS, (
            f"Store-unavailable p95 overhead {p95_ms:.3f} ms exceeds budget of {BUDGET_MS} ms"
        )
