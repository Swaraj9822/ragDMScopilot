"""Unit tests for trace/span context propagation edge cases.

Feature: ai-observability-platform (task 2.3).

These tests exercise two edge cases of
``src/rag_system/observability_tracing/context.py``:

* R2.2 - WHERE no Trace is active, the active trace_id and active span_id resolve
  to a null value. Exercised on a clean context where nothing has been set.
* R2.5 - IF trace-context propagation to a background thread fails, THEN the work
  runs with null trace/span identity, an error indication is recorded (the
  ``rag_trace_context_propagation_failures_total`` counter is incremented), and
  the background work is allowed to proceed.

The propagation-failure path is forced by monkeypatching
``contextvars.copy_context`` (as imported into the context module) so it raises.
The failure counter is read back through the ``MetricsRegistry`` public Prometheus
text-exposition API (``render_prometheus``).
"""

import contextvars

import pytest

from rag_system.observability import metrics, reset_trace_id, set_trace_id
from rag_system.observability_tracing import (
    bind_span,
    get_active_span_id,
    get_active_trace_id,
    propagate_into_thread,
    restore_span,
)
from rag_system.observability_tracing import context as context_module

_FAILURES_METRIC = "rag_trace_context_propagation_failures_total"


def _read_counter(name: str) -> float:
    """Read a label-less counter value from the public Prometheus exposition.

    Returns 0.0 when the counter has never been incremented (i.e. it is absent
    from the rendered output).
    """
    prefix = f"{name} "
    for line in metrics.render_prometheus().splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix):])
    return 0.0


# ---------------------------------------------------------------------------
# R2.2 - null-context default when no trace/span is active.
# ---------------------------------------------------------------------------


def test_active_ids_are_none_on_clean_context() -> None:
    """With nothing set, both active accessors resolve to null (R2.2)."""
    # Run inside a fresh, isolated context copy so this assertion is independent
    # of any trace/span an outer scope might have left active.
    def _observe() -> tuple[str | None, str | None]:
        return get_active_trace_id(), get_active_span_id()

    trace_id, span_id = contextvars.copy_context().run(_observe)

    assert trace_id is None
    assert span_id is None


def test_active_span_id_is_none_after_trace_set_without_span() -> None:
    """Setting only a trace leaves the active span_id null (R2.2)."""
    trace_token = set_trace_id("abc123")
    try:
        assert get_active_trace_id() == "abc123"
        assert get_active_span_id() is None
    finally:
        reset_trace_id(trace_token)


# ---------------------------------------------------------------------------
# R2.5 - propagation failure runs with null context and records the metric.
# ---------------------------------------------------------------------------


def test_propagation_failure_runs_null_context_and_increments_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy_context failure runs the work with null identity and counts it (R2.5).

    The dispatching context has an active trace and span. When the snapshot fails
    the wrapped callable must still execute, observe a null trace_id and span_id,
    and the propagation-failure counter must increase by exactly one.
    """
    before = _read_counter(_FAILURES_METRIC)

    # Force the propagation snapshot to fail wherever the context module uses it.
    def _boom() -> contextvars.Context:
        raise RuntimeError("forced copy_context failure")

    monkeypatch.setattr(context_module, "copy_context", _boom)

    observed: dict[str, str | None] = {}

    def _work() -> str:
        # Identity observed from inside the propagated (failed) work must be null.
        observed["trace_id"] = get_active_trace_id()
        observed["span_id"] = get_active_span_id()
        return "done"

    # Establish a real active trace/span on the dispatching thread, so a null
    # observation inside the work can only come from the failure fallback.
    trace_token = set_trace_id("trace-xyz")
    span_token = bind_span("span-xyz")
    try:
        wrapped = propagate_into_thread(_work)
        result = wrapped()
    finally:
        restore_span(span_token)
        reset_trace_id(trace_token)

    # The wrapped callable still ran to completion.
    assert result == "done"

    # It executed with null trace/span identity despite an active outer context.
    assert observed["trace_id"] is None
    assert observed["span_id"] is None

    # An error indication was recorded: the failure counter advanced by one.
    after = _read_counter(_FAILURES_METRIC)
    assert after == before + 1.0


def test_copy_context_restored_after_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the monkeypatch is undone, propagation works normally again.

    Guards against the failure-path test leaving a broken ``copy_context`` in the
    module: with the real implementation restored, an active context propagates
    through and no failure is counted.
    """
    # Sanity: monkeypatch then immediately let pytest tear it down via undo().
    monkeypatch.setattr(context_module, "copy_context", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.undo()

    before = _read_counter(_FAILURES_METRIC)

    trace_token = set_trace_id("trace-ok")
    span_token = bind_span("span-ok")
    try:
        wrapped = propagate_into_thread(lambda: (get_active_trace_id(), get_active_span_id()))
        trace_id, span_id = wrapped()
    finally:
        restore_span(span_token)
        reset_trace_id(trace_token)

    # Real propagation carried the active identity across.
    assert trace_id == "trace-ok"
    assert span_id == "span-ok"

    # No propagation failure recorded on the healthy path.
    assert _read_counter(_FAILURES_METRIC) == before
