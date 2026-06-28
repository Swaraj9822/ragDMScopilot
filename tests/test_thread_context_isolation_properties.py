"""Property test for thread-context isolation across pooled work.

Feature: ai-observability-platform.

This module exercises ``propagate_into_thread`` from
``src/rag_system/observability_tracing/context.py`` (task 2.1). That wrapper
snapshots the *current* trace/span context (the ``rag_trace_id`` ContextVar from
``rag_system.observability`` plus the sibling ``_ACTIVE_SPAN_ID``) and runs the
wrapped callable inside an isolated copy of that snapshot. Because the snapshot
runs in its own context copy, it must never mutate the pooled worker thread's
own context - so once propagated work finishes, later *unrelated* work scheduled
onto the same pooled thread observes a null trace_id and span_id (R2.4).

The test dispatches a sequence of trace/span contexts onto a shared
``ThreadPoolExecutor`` constrained to a single worker. A single worker guarantees
the worker thread is reused for every task, so each "bare" callable provably runs
on the same pooled thread that just executed propagated work - exactly the
leakage scenario Property 31 rules out.
"""

import string
import threading
from concurrent.futures import ThreadPoolExecutor

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability import reset_trace_id, set_trace_id
from rag_system.observability_tracing import (
    bind_span,
    get_active_span_id,
    get_active_trace_id,
    propagate_into_thread,
    restore_span,
)

# ---------------------------------------------------------------------------
# Smart generators - constrained to the realistic identity input domain.
# ---------------------------------------------------------------------------

# trace_id / span_id are opaque, non-empty identifier strings carried through
# the ContextVars; a hex-ish alphabet keeps them well-formed without narrowing
# the property's scope.
_identity_ids = st.text(
    alphabet=string.hexdigits.lower() + "-",
    min_size=1,
    max_size=32,
)

# A sequence of (trace_id, span_id) contexts to dispatch one after another onto
# the same pooled thread, so repeated propagate-then-bare cycles are exercised.
_context_sequences = st.lists(
    st.tuples(_identity_ids, _identity_ids),
    min_size=1,
    max_size=8,
)


def _observe_context() -> tuple[str | None, str | None, int]:
    """Return the trace/span identity visible here plus the running thread id."""
    return (get_active_trace_id(), get_active_span_id(), threading.get_ident())


# ---------------------------------------------------------------------------
# Property 31 - thread context does not leak across pooled work.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 31: Thread context does not leak across pooled work
# Validates: Requirements 2.4
@settings(max_examples=100)
@given(contexts=_context_sequences)
def test_propagated_context_does_not_leak_across_pooled_work(
    contexts: list[tuple[str, str]],
) -> None:
    """Bare work on a pooled thread sees null identity after propagated work.

    R2.4 / Property 31: for any sequence of trace/span contexts dispatched onto a
    shared, single-worker pool, after a propagated callable runs with an active
    context, a subsequent bare callable scheduled on the same pooled thread
    observes a null trace_id and span_id - the propagated identity never leaks.
    """
    # A single worker forces thread reuse, so the bare callable provably runs on
    # the same pooled thread that just executed propagated work.
    with ThreadPoolExecutor(max_workers=1) as pool:
        for trace_id, span_id in contexts:
            # Establish an active trace/span context on the dispatching thread.
            trace_token = set_trace_id(trace_id)
            span_token = bind_span(span_id)
            try:
                propagated = propagate_into_thread(_observe_context)
                prop_trace, prop_span, prop_thread = pool.submit(propagated).result()
            finally:
                # Tear the context back down on the dispatching thread.
                restore_span(span_token)
                reset_trace_id(trace_token)

            # Sanity: propagation actually carried the active identity across,
            # otherwise the no-leak assertion below would be vacuous.
            assert prop_trace == trace_id
            assert prop_span == span_id

            # Now schedule unrelated, unwrapped work onto the same pool. With no
            # context active on the dispatching thread and no propagation wrapper,
            # the pooled thread must observe a fully null identity.
            bare_trace, bare_span, bare_thread = pool.submit(_observe_context).result()

            # The bare work ran on the very thread that just held a propagated
            # context, so this is a genuine leakage check.
            assert bare_thread == prop_thread
            assert bare_trace is None
            assert bare_span is None
