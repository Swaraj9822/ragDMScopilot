"""Property test for concurrent branch attachment to the originating trace.

Feature: ai-observability-platform, Property 30: Concurrent branches attach spans to the originating trace

This module validates that when ``propagate_into_thread`` wraps work dispatched
to a ``ThreadPoolExecutor``, any spans created inside those threads are attached
to the originating trace - i.e. both ``get_active_trace_id()`` and
``get_active_span_id()`` return the values that were active on the dispatching
thread at wrap time.

Additionally, it verifies that the propagated context does not leak: a plain
(unwrapped) callable submitted afterward on the same pooled thread observes
``None`` for both trace and span identity.

Requirements covered:

* R2.3 — WHEN work is dispatched to a background thread, THE
  Trace_Context_Propagator SHALL make the originating trace_id and active span_id
  available to that thread before the dispatched work begins executing.
* R2.6 — WHEN a hybrid query runs the RAG and copilot branches concurrently, THE
  Span_Recorder SHALL attach every Span created in each branch to the Trace
  identified by the originating trace_id.
"""

import string
from concurrent.futures import ThreadPoolExecutor, as_completed

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import reset_trace_id, set_trace_id
from rag_system.observability_tracing.context import (
    bind_span,
    get_active_span_id,
    get_active_trace_id,
    propagate_into_thread,
    restore_span,
    set_recording,
    reset_recording,
)

# ---------------------------------------------------------------------------
# Strategies — constrained to realistic trace/span identity domain.
# ---------------------------------------------------------------------------

# trace_id: 32-char lowercase hex strings (realistic domain).
_trace_ids = st.text(
    alphabet=string.hexdigits[:16],  # lowercase hex chars
    min_size=32,
    max_size=32,
)

# span_id: non-empty identifier strings (hex-ish).
_span_ids = st.text(
    alphabet=string.hexdigits[:16] + "-",
    min_size=1,
    max_size=32,
)

# Number of concurrent branches to dispatch (simulates RAG + copilot + extras).
_branch_counts = st.integers(min_value=2, max_value=8)


# ---------------------------------------------------------------------------
# Property 30 — Concurrent branches attach spans to the originating trace.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 30: Concurrent branches attach spans to the originating trace
# Validates: Requirements 2.3, 2.6
@settings(max_examples=100)
@given(
    trace_id=_trace_ids,
    span_id=_span_ids,
    num_branches=_branch_counts,
)
def test_concurrent_branches_attach_to_originating_trace(
    trace_id: str,
    span_id: str,
    num_branches: int,
) -> None:
    """All concurrent branches see the originating trace_id and span_id.

    R2.3 / R2.6 / Property 30: for any originating trace context dispatched into
    multiple concurrent branches via ``propagate_into_thread``, every branch
    callable observes the originating trace_id and active span_id. After the
    propagated work completes, a subsequent plain callable on the same pool does
    NOT see the propagated context (no leakage).
    """

    # 1. Set up the originating trace context on the dispatching thread.
    trace_token = set_trace_id(trace_id)
    span_token = bind_span(span_id)
    recording_token = set_recording(True)

    try:
        # 2. Wrap multiple callables with propagate_into_thread (simulating
        #    concurrent RAG and copilot branches in a hybrid query).
        def branch_work() -> tuple[str | None, str | None]:
            """Simulate work inside a branch — observe trace/span identity."""
            return (get_active_trace_id(), get_active_span_id())

        wrapped_branches = [propagate_into_thread(branch_work) for _ in range(num_branches)]

        # 3. Submit them concurrently to a ThreadPoolExecutor.
        with ThreadPoolExecutor(max_workers=num_branches) as pool:
            futures = [pool.submit(wrapped) for wrapped in wrapped_branches]

            # 4. Assert every branch sees the originating trace_id and span_id.
            for future in as_completed(futures):
                observed_trace, observed_span = future.result()
                assert observed_trace == trace_id, (
                    f"Branch should see originating trace_id={trace_id!r}, "
                    f"got {observed_trace!r}"
                )
                assert observed_span == span_id, (
                    f"Branch should see originating span_id={span_id!r}, "
                    f"got {observed_span!r}"
                )

            # 5. Verify no leakage: submit a plain (unwrapped) callable onto the
            #    pool — it should see None for both trace and span identity.
            def plain_work() -> tuple[str | None, str | None]:
                return (get_active_trace_id(), get_active_span_id())

            plain_trace, plain_span = pool.submit(plain_work).result()
            assert plain_trace is None, (
                f"Plain callable should see trace_id=None after propagated work, "
                f"got {plain_trace!r}"
            )
            assert plain_span is None, (
                f"Plain callable should see span_id=None after propagated work, "
                f"got {plain_span!r}"
            )

    finally:
        # Tear down the originating context on the dispatching thread.
        reset_recording(recording_token)
        restore_span(span_token)
        reset_trace_id(trace_token)
