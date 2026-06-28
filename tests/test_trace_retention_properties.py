"""Property test for trace (and log) retention.

Feature: ai-observability-platform.

This module exercises retention through the in-memory transactional store double
(``tests/observability_tracing_store_double.py``), the same double the spec
designates for the persistence/retention property tests (task 8.2). It validates
the design's Property 32 / R13.1, R13.2, R18.1:

- a retention cycle removes **exactly** those entries whose age
  (``reference - start_ts``) is *strictly greater* than the configured period
  and retains those whose age is *less than or equal to* the period -- so an
  entry sitting exactly on the boundary is kept (R13.1, R18.1);
- when a Trace is removed, **all of its Spans are removed in the same cycle**,
  leaving no orphan spans, while every retained Trace keeps its complete span
  set unchanged (R13.2).

Both the trace store (``enforce_retention``) and the log store
(``enforce_log_retention``) implement the same strictly-older semantics, so the
single property below drives both: R13.1/R13.2 over traces+spans and R18.1 over
log records.

Ages and the retention period are drawn from a shared offset pool measured
against a fixed reference time, so the inclusive boundary (age == period) is hit
routinely rather than by accident, and every cycle contains a mix of
strictly-older (removed), boundary (retained), and younger (retained) entries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import LogRecordModel, Span, Trace

# Test-support module (not collected by pytest); imported by module name, the
# repo's convention for shared test fakes (see test_preservation_properties.py).
from observability_tracing_store_double import InMemoryTransactionalStore

# ---------------------------------------------------------------------------
# Smart generators - constrained to the retention domain.
# ---------------------------------------------------------------------------

# A fixed reference "now" passed explicitly to the retention cycle so the test is
# deterministic and independent of wall-clock time.
_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# Age offsets (in seconds) shared by entry ages and the retention period. Because
# ages and the period are drawn from the same pool, the boundary case
# (age == period) occurs frequently, exercising the inclusive-retain rule.
_AGE_OFFSETS = (0, 60, 3600, 7200, 86_400, 172_800)

_ages = st.sampled_from(_AGE_OFFSETS)
_periods = st.sampled_from([o for o in _AGE_OFFSETS if o > 0])

_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _span(trace_id: str, index: int) -> Span:
    """A minimal valid child span; span_id unique within its trace."""
    return Span(
        span_id=f"{trace_id}-s{index}",
        parent_span_id=None if index == 0 else f"{trace_id}-s0",
        operation=f"stage-{index}",
        start_ts=_NOW,
        duration_ms=index,
        status="success",
        attributes={},
    )


@st.composite
def _traces(draw: st.DrawFn) -> list[Trace]:
    """Build 0..N traces with unique ids, each carrying 1..4 spans.

    Each trace's ``start_ts`` is ``_NOW`` minus an age drawn from the shared
    offset pool, so the population spans strictly-older, boundary, and younger
    entries relative to whatever period the cycle uses.
    """
    count = draw(st.integers(min_value=0, max_value=20))
    traces: list[Trace] = []
    for index in range(count):
        trace_id = f"{index:032x}"
        age = draw(_ages)
        span_count = draw(st.integers(min_value=1, max_value=4))
        traces.append(
            Trace(
                trace_id=trace_id,
                route="/ask",
                start_ts=_NOW - timedelta(seconds=age),
                duration_ms=age,
                root_status="success",
                spans=[_span(trace_id, i) for i in range(span_count)],
            )
        )
    return traces


@st.composite
def _logs(draw: st.DrawFn) -> list[LogRecordModel]:
    """Build 0..N log records with timestamps drawn from the shared age pool."""
    count = draw(st.integers(min_value=0, max_value=20))
    logs: list[LogRecordModel] = []
    for index in range(count):
        age = draw(_ages)
        logs.append(
            LogRecordModel(
                timestamp=_NOW - timedelta(seconds=age),
                level=draw(st.sampled_from(_LEVELS)),
                logger="rag",
                message=f"m{index}",
                trace_id=f"{index:032x}",
                exc_text=None,
                extra={},
            )
        )
    return logs


# ---------------------------------------------------------------------------
# Property 32 - retention removes strictly-older entries and cascades to spans.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 32: Retention removes strictly-older entries and cascades to spans
# Validates: Requirements 13.1, 13.2, 18.1
@settings(max_examples=100)
@given(traces=_traces(), logs=_logs(), period_seconds=_periods)
def test_retention_removes_strictly_older_and_cascades_to_spans(
    traces: list[Trace],
    logs: list[LogRecordModel],
    period_seconds: int,
) -> None:
    """A retention cycle keeps exactly the entries within the period.

    For both traces (with their spans) and log records, an entry is removed iff
    its age relative to the reference time is strictly greater than the period;
    boundary entries (age == period) are retained (R13.1, R18.1). Removed traces
    take all of their spans with them, leaving no orphan spans, while retained
    traces keep their complete span set (R13.2).
    """
    max_age = timedelta(seconds=period_seconds)
    store = InMemoryTransactionalStore()
    for trace in traces:
        store.persist(trace)
    for record in logs:
        store.persist_log(record)

    # Oracle: an entry is retained iff age <= period (strictly-older removed).
    expected_trace_ids = {
        t.trace_id for t in traces if _NOW - t.start_ts <= max_age
    }
    expected_log_seqs = {
        # insertion_seq is assigned 1..N in persist order; recompute the same way.
        i + 1
        for i, record in enumerate(logs)
        if _NOW - record.timestamp <= max_age
    }

    store.enforce_retention(max_age, now=_NOW)
    store.enforce_log_retention(max_age, now=_NOW)

    # --- Traces: exactly the within-period traces survive (R13.1, R18.1) ------
    surviving = store.search_traces(_all_traces_filter())
    surviving_ids = {t.trace_id for t in surviving}
    assert surviving_ids == expected_trace_ids

    # --- Cascade: removed traces (and their spans) are gone; retained traces
    #     keep their full span set with no orphan spans left behind (R13.2). ---
    original_by_id = {t.trace_id: t for t in traces}
    for trace_id in original_by_id:
        fetched = store.get_trace(trace_id)
        if trace_id in expected_trace_ids:
            assert fetched is not None
            expected_span_ids = {s.span_id for s in original_by_id[trace_id].spans}
            assert {s.span_id for s in fetched.spans} == expected_span_ids
        else:
            # Trace removed -> its spans are removed in the same cycle (no orphans).
            assert fetched is None

    # --- Logs: exactly the within-period records survive (R18.1) --------------
    surviving_logs = store.search_logs(_all_logs_filter())
    surviving_log_seqs = {r.insertion_seq for r in surviving_logs}
    assert surviving_log_seqs == expected_log_seqs


# ---------------------------------------------------------------------------
# Helpers: filters that match every entry so search reports the full survivor
# set without truncation (limit pinned to the store maximum).
# ---------------------------------------------------------------------------


def _all_traces_filter():
    from observability_tracing_store_double import TraceSearchFilters

    return TraceSearchFilters(limit=1000)


def _all_logs_filter():
    from observability_tracing_store_double import LogSearchFilters

    return LogSearchFilters(limit=1000)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
