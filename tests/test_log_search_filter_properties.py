"""Property test for log search returning exactly the records satisfying every
filter.

Feature: ai-observability-platform.

This module exercises log search through the in-memory transactional store
double (``tests/observability_tracing_store_double.py``), the same double the
spec designates for the persistence/query property tests (task 8.2). For any set
of persisted Log_Records and any combination of time-range, Log_Level, and
trace_id filters, ``search_logs`` must return **exactly** the set of records that
satisfy every supplied filter simultaneously, per the design's Property 14 /
R16.1-R16.4:

- record timestamp within the inclusive ``[start, end]`` range (R16.1);
- Log_Level exactly equal under a case-sensitive comparison (R16.2);
- trace_id exactly equal under a case-sensitive comparison (R16.3);
- all supplied filters combined with AND semantics (R16.4).

This property isolates the *filtering* behaviour from ordering/limit (Property
15, task 10.4): every search runs with a result limit large enough (1000, the
store's maximum) that no match is ever truncated, so the returned set can be
compared for set equality against an independent oracle that re-applies the same
predicates. To exercise case-sensitivity, levels and trace_ids are drawn from
pools that include case variants (e.g. ``"INFO"`` vs ``"info"``, a lowercase hex
trace_id vs its uppercase form), and filter values are drawn from those same
pools plus values that match nothing.

Timestamps and filter bounds are drawn from a small shared offset pool so range
filters frequently bisect the data (rather than trivially including or excluding
everything), making the inclusive-boundary behaviour a routinely exercised part
of the property. Records are keyed for identity by their ``insertion_seq`` (the
store assigns a unique, monotonically increasing sequence on commit), so the
returned set can be compared exactly even when timestamps and other fields
collide.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import LogRecordModel

# Test-support module (not collected by pytest); imported by module name, the
# repo's convention for shared test fakes (see test_preservation_properties.py).
from observability_tracing_store_double import (
    InMemoryTransactionalStore,
    LogSearchFilters,
)

# ---------------------------------------------------------------------------
# Smart generators - constrained to the log-search domain.
# ---------------------------------------------------------------------------

# A small UTC timestamp pool shared by record timestamps and filter bounds, so
# range filters routinely bisect the data and the inclusive boundaries (R16.1)
# are frequently hit rather than trivially satisfied.
_BASE_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TS_OFFSETS = (0, 10, 20, 30, 40, 50)
_ts_pool = st.sampled_from([_BASE_TS + timedelta(seconds=o) for o in _TS_OFFSETS])

# Level pool includes case variants so the case-sensitive comparison (R16.2) is
# meaningfully exercised: "INFO" must not match "info".
_LEVEL_POOL = ["DEBUG", "INFO", "WARNING", "ERROR", "info", "Error"]
_levels = st.sampled_from(_LEVEL_POOL)

# trace_id pool includes a lowercase 32-hex id and its uppercase form so the
# case-sensitive comparison (R16.3) is exercised, plus ``None`` for records that
# carry no trace correlation (R14.3).
_TRACE_LOWER = "0123456789abcdef0123456789abcdef"
_TRACE_UPPER = _TRACE_LOWER.upper()
_OTHER_TRACE = "fedcba9876543210fedcba9876543210"
_TRACE_POOL = [_TRACE_LOWER, _TRACE_UPPER, _OTHER_TRACE, None]
_trace_ids = st.sampled_from(_TRACE_POOL)


@st.composite
def _records(draw: st.DrawFn) -> list[LogRecordModel]:
    """Build a list of 0..N log records.

    Only the fields read by search filtering (timestamp, level, trace_id) vary
    meaningfully; logger/message/exc_text/extra are held to fixed placeholders
    because they do not participate in the filter predicate. ``insertion_seq`` is
    left at its default 0 here; the store assigns the authoritative unique
    sequence on commit, which the test uses as the record identity.
    """
    count = draw(st.integers(min_value=0, max_value=25))
    records: list[LogRecordModel] = []
    for _ in range(count):
        records.append(
            LogRecordModel(
                timestamp=draw(_ts_pool),
                level=draw(_levels),
                logger="rag_system.test",
                message="m",
                trace_id=draw(_trace_ids),
                exc_text=None,
            )
        )
    return records


# Filter component generators: each filter is independently present or absent so
# every combination (including the no-filter case) is reachable.
_maybe_ts = st.one_of(st.none(), _ts_pool)
# Level/trace_id filter values include the in-use pools plus a value that matches
# nothing, so both "matches some" and "matches none" paths are covered. The
# trace_id filter value is never ``None`` (a ``None`` filter means "not supplied"
# in the store double, so it cannot be used to select null-trace_id records).
_maybe_level = st.one_of(st.none(), st.sampled_from([*_LEVEL_POOL, "CRITICAL"]))
_maybe_trace = st.one_of(
    st.none(), st.sampled_from([_TRACE_LOWER, _TRACE_UPPER, _OTHER_TRACE, "nomatch"])
)


@st.composite
def _filters(draw: st.DrawFn) -> LogSearchFilters:
    """Build a search filter set with each filter independently present/absent.

    The limit is pinned to 1000 (the store maximum) so filtering is never masked
    by truncation; ordering and limit are validated separately by Property 15.
    """
    return LogSearchFilters(
        start=draw(_maybe_ts),
        end=draw(_maybe_ts),
        level=draw(_maybe_level),
        trace_id=draw(_maybe_trace),
        limit=1000,
    )


def _satisfies(record: LogRecordModel, filters: LogSearchFilters) -> bool:
    """Independent oracle: True iff *record* satisfies every supplied filter.

    Re-states R16.1-R16.3 directly (inclusive range, case-sensitive level and
    trace_id equality) combined with AND semantics (R16.4). A ``None`` filter
    component imposes no constraint.
    """
    if filters.start is not None and record.timestamp < filters.start:
        return False
    if filters.end is not None and record.timestamp > filters.end:
        return False
    if filters.level is not None and record.level != filters.level:
        return False
    if filters.trace_id is not None and record.trace_id != filters.trace_id:
        return False
    return True


# ---------------------------------------------------------------------------
# Property 14 - log search returns exactly the records satisfying every filter.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 14: Log search returns exactly the records satisfying every filter
# Validates: Requirements 16.1, 16.2, 16.3, 16.4
@settings(max_examples=100)
@given(records=_records(), filters=_filters())
def test_log_search_returns_exactly_matching_records(
    records: list[LogRecordModel], filters: LogSearchFilters
) -> None:
    """``search_logs`` returns exactly the records satisfying every filter.

    With a limit large enough that no match is truncated, the returned set of
    records (identified by the store-assigned ``insertion_seq``) must equal the
    set computed by an independent oracle applying the inclusive time range
    (R16.1), case-sensitive level (R16.2) and trace_id (R16.3) equality, all
    combined with AND semantics (R16.4).
    """
    store = InMemoryTransactionalStore()
    for record in records:
        store.persist_log(record)

    results = store.search_logs(filters)

    # The store assigns a unique insertion_seq on commit, in persist order, so
    # the i-th persisted record carries insertion_seq == i + 1. Build the oracle
    # set of expected sequences from the same predicate.
    expected_seqs = {
        index + 1 for index, record in enumerate(records) if _satisfies(record, filters)
    }
    returned_seqs = {r.insertion_seq for r in results}

    # Exactly the matching records: none missing, none extra.
    assert returned_seqs == expected_seqs
    # No record is returned more than once.
    assert len(results) == len(returned_seqs)
    # Every returned record genuinely satisfies every supplied filter (AND).
    for record in results:
        assert _satisfies(record, filters)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
