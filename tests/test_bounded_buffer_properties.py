"""Property tests for the bounded in-memory span/log buffers.

Feature: ai-observability-platform.

These tests exercise the overflow contract of the bounded buffers defined in
``src/rag_system/observability_tracing/buffers.py`` (task 6.x): a buffer holds
at most ``capacity`` entries, drops every *newly offered* entry beyond capacity
(retaining the already-buffered ones), increments a dropped counter on the
injected :class:`~rag_system.observability.MetricsRegistry` by exactly the
number of dropped entries, and never raises on overflow.

A fresh, isolated ``MetricsRegistry`` is injected per example so the dropped
counter assertion is deterministic and independent of the process-wide default
registry. The counter value is read back through the registry's public
Prometheus text exposition (``render_prometheus``).
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import MetricsRegistry
from rag_system.observability_tracing.buffers import (
    BoundedBuffer,
    BoundedLogBuffer,
    BoundedSpanBuffer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_counter(registry: MetricsRegistry, name: str) -> float:
    """Read an unlabeled counter's value via the registry's public API.

    The dropped counters carry no labels, so they render as a bare
    ``<name> <value>`` line. A counter that was never incremented is absent
    from the exposition, which is reported here as ``0.0``.
    """
    prefix = f"{name} "
    for line in registry.render_prometheus().splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix):])
    return 0.0


# Each case pairs a buffer factory (capacity, registry) -> buffer with the
# dropped-counter metric name that buffer is expected to increment on overflow.
# The property applies identically to the span buffer, the log buffer, and the
# generic base buffer.
_BUFFER_CASES = [
    (
        lambda cap, reg: BoundedSpanBuffer(capacity=cap, metrics=reg),
        "rag_spans_dropped_total",
    ),
    (
        lambda cap, reg: BoundedLogBuffer(capacity=cap, metrics=reg),
        "rag_logs_dropped_total",
    ),
    (
        lambda cap, reg: BoundedBuffer(
            "rag_generic_dropped_total", capacity=cap, metrics=reg
        ),
        "rag_generic_dropped_total",
    ),
]

# Smart generators: small capacities and counts keep examples fast while still
# covering the under-capacity, at-capacity, and over-capacity regimes.
_capacities = st.integers(min_value=1, max_value=50)
_item_counts = st.integers(min_value=0, max_value=100)
_buffer_cases = st.sampled_from(_BUFFER_CASES)


# ---------------------------------------------------------------------------
# Property 19 - bounded buffer caps size and counts drops on overflow.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 19: Bounded buffer caps size and counts drops on overflow
# Validates: Requirements 9.4, 9.5, 17.3, 17.4
@settings(max_examples=100)
@given(case=_buffer_cases, capacity=_capacities, n_items=_item_counts)
def test_bounded_buffer_caps_size_and_counts_drops(
    case: tuple, capacity: int, n_items: int
) -> None:
    """For any capacity and N added items the buffer never overflows.

    Asserts the four guarantees of R9.4/R9.5/R17.3/R17.4:
    - the buffer never holds more than ``capacity`` entries;
    - it retains exactly ``min(N, capacity)`` entries;
    - the dropped counter increases by exactly ``max(0, N - capacity)``;
    - ``add`` never raises on overflow (returning ``False`` instead).
    """
    factory, metric_name = case
    registry = MetricsRegistry()
    buffer = factory(capacity, registry)

    # Counter starts clean on the isolated registry.
    assert _read_counter(registry, metric_name) == 0.0

    buffered = 0
    dropped = 0
    for i in range(n_items):
        try:
            accepted = buffer.add(i)
        except Exception as exc:  # add must never raise, even on overflow.
            raise AssertionError(f"add() raised on overflow: {exc!r}") from exc

        if accepted:
            buffered += 1
            # Acceptance only happens while there is remaining capacity.
            assert i < capacity
        else:
            dropped += 1
            # A drop only happens once the buffer is already at capacity.
            assert i >= capacity

        # Invariant after every offer: size never exceeds capacity.
        assert len(buffer) <= capacity

    expected_retained = min(n_items, capacity)
    expected_dropped = max(0, n_items - capacity)

    assert buffered == expected_retained
    assert dropped == expected_dropped
    assert len(buffer) == expected_retained
    assert _read_counter(registry, metric_name) == float(expected_dropped)

    # drain() hands back exactly the retained entries and clears the buffer.
    drained = buffer.drain()
    assert len(drained) == expected_retained
    assert len(buffer) == 0
