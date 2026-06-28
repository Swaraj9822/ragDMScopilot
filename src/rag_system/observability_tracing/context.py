"""Trace/span context propagation for the AI observability platform.

Owns trace/span identity propagation. Reuses the existing ``_TRACE_ID``
``ContextVar`` from :mod:`rag_system.observability` for the trace id and adds a
sibling ``_ACTIVE_SPAN_ID`` for the active span id. Together they make request
identity follow execution across threads (e.g. the hybrid query's concurrent
RAG/copilot branches).

Requirements covered:

* R2.1 / R2.2 — expose the active trace_id / span_id, resolving to ``None`` when
  no trace or span is active.
* R2.3 / R2.4 — :func:`propagate_into_thread` copies the current context into the
  target thread before the work runs and leaves the pooled thread's own context
  untouched afterward, so identity does not leak to later work.
* R2.5 — on a propagation failure the work runs with null context, an error
  metric (``rag_trace_context_propagation_failures_total``) is incremented, and
  execution proceeds.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar, Token, copy_context
from typing import Any, Callable, TypeVar

from rag_system.observability import _TRACE_ID, get_trace_id, metrics

# Sibling of the existing ``rag_trace_id`` ContextVar (R2.1).
_ACTIVE_SPAN_ID: ContextVar[str | None] = ContextVar("rag_active_span_id", default=None)

# Tracks whether the enclosing trace is actually being recorded. ``start_trace``
# sets this True only when the sampler accepts the trace; otherwise it is False
# (the default), so spans opened while tracing is disabled / not sampled are
# skipped entirely rather than created and enqueued (R10.1).
_RECORDING: ContextVar[bool] = ContextVar("rag_trace_recording", default=False)

# Counter name incremented when thread context propagation fails (R2.5).
_PROPAGATION_FAILURES_METRIC = "rag_trace_context_propagation_failures_total"

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Active identity accessors
# ---------------------------------------------------------------------------


def get_active_trace_id() -> str | None:
    """Return the active trace_id, or ``None`` when no trace is active (R2.1, R2.2).

    Delegates to the existing :func:`rag_system.observability.get_trace_id` so
    the trace id stays consistent with the ``rag_trace_id`` context variable.
    """
    return get_trace_id()


def get_active_span_id() -> str | None:
    """Return the active span_id, or ``None`` when no span is active (R2.1, R2.2)."""
    return _ACTIVE_SPAN_ID.get()


# ---------------------------------------------------------------------------
# Span binding / restoration
# ---------------------------------------------------------------------------


def bind_span(span_id: str) -> Token[str | None]:
    """Set *span_id* as the active span and return a reset token.

    The token is later handed to :func:`restore_span` to re-establish the parent
    span once the child completes.
    """
    return _ACTIVE_SPAN_ID.set(span_id)


def restore_span(token: Token[str | None]) -> None:
    """Restore the parent span identified by *token* (R1.6)."""
    _ACTIVE_SPAN_ID.reset(token)


# ---------------------------------------------------------------------------
# Recording state
# ---------------------------------------------------------------------------


def is_recording() -> bool:
    """Return whether the enclosing trace is currently being recorded (R10.1).

    Defaults to ``False`` so that, absent an active sampled trace, span creation
    is skipped entirely. ``start_trace`` flips this to ``True`` only for the
    duration of a sampled trace.
    """
    return _RECORDING.get()


def set_recording(recording: bool) -> Token[bool]:
    """Mark the current context's recording state and return a reset token."""
    return _RECORDING.set(recording)


def reset_recording(token: Token[bool]) -> None:
    """Restore the prior recording state identified by *token*."""
    _RECORDING.reset(token)


# ---------------------------------------------------------------------------
# Cross-thread propagation
# ---------------------------------------------------------------------------


def _invoke_in_null_context(fn: Callable[..., _T], args: tuple, kwargs: dict) -> _T:
    """Run *fn* with both trace and span identity resolved to null."""
    _TRACE_ID.set(None)
    _ACTIVE_SPAN_ID.set(None)
    _RECORDING.set(False)
    return fn(*args, **kwargs)


def _run_with_null_context(fn: Callable[..., _T], args: tuple, kwargs: dict) -> _T:
    """Execute *fn* with null trace/span context without leaking into the pool.

    Prefers an isolated copied context; falls back to manual set/reset using
    tokens so the calling (pooled) thread's prior context is always restored.
    """
    try:
        isolated = copy_context()
    except Exception:
        isolated = None

    if isolated is not None:
        return isolated.run(_invoke_in_null_context, fn, args, kwargs)

    trace_token = _TRACE_ID.set(None)
    span_token = _ACTIVE_SPAN_ID.set(None)
    recording_token = _RECORDING.set(False)
    try:
        return fn(*args, **kwargs)
    finally:
        _TRACE_ID.reset(trace_token)
        _ACTIVE_SPAN_ID.reset(span_token)
        _RECORDING.reset(recording_token)


def propagate_into_thread(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Wrap *fn* so the current trace/span context is carried into a worker thread.

    The current context is snapshotted at wrap time via
    :func:`contextvars.copy_context`. When the wrapped callable runs (typically on
    a pooled worker thread) it executes inside that snapshot, so the originating
    trace_id and active span_id are available to the work (R2.3) while the pooled
    thread's own context is left untouched and does not leak to later work (R2.4).

    If snapshotting the context fails, the work runs with null trace/span context,
    the ``rag_trace_context_propagation_failures_total`` counter is incremented,
    and execution proceeds (R2.5).
    """
    try:
        captured = copy_context()
    except Exception:
        metrics.increment(_PROPAGATION_FAILURES_METRIC)
        captured = None

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        if captured is None:
            return _run_with_null_context(fn, args, kwargs)
        return captured.run(fn, *args, **kwargs)

    return wrapper
