"""Property test for log search ordering and result-limit behaviour.

Feature: ai-observability-platform.

This module exercises log search through the in-memory transactional store
double (``tests/observability_tracing_store_double.py``), the same double the
spec designates for the persistence/query property tests (task 8.2). It isolates
the *ordering and limit* behaviour of ``search_logs`` (design Property 15 /
R16.5, R16.6) from the filtering behaviour validated separately by Property 14
(task 10.3):

- when a search omits a result limit, the service returns at most 100 records
  ordered by record timestamp in descending order (R16.5);
- when a search supplies a valid result limit (an integer in 1..1000), the
  service returns at most that many records ordered by record timestamp in
  descending order (R16.6).

To make truncation a routinely exercised path, the number of generated records
can comfortably exceed the limits under test, so the cap actually bites. Record
timestamps are drawn from a small shared pool so ties are common, ensuring the
descending-by-timestamp ordering (with the insertion-order tiebreaker, R15.2) is
checked in the presence of equal timestamps. No filters are applied, so every
stored record is a candidate and the returned count is governed purely by the
effective limit.
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
# Smart generators - constrained to the ordering/limit domain.
# ---------------------------------------------------------------------------

# Default limit applied when a search omits one (R16.5), and the maximum a client
# may supply (R16.6). Mirrors the store double's _DEFAULT_LIMIT / _MAX_LIMIT.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000

# A small UTC timestamp pool shared by all records so equal timestamps are
# common; this forces the descending-by-timestamp ordering (R16.5, R16.6) to be
# exercised in the presence of ties, where the insertion-order tiebreaker
# (R15.2) decides the relative order.
_BASE_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TS_OFFSETS = (0, 5, 10, 15, 20)
_ts_pool = st.sampled_from([_BASE_TS + timedelta(seconds=o) for o in _TS_OFFSETS])

# Level/logger/trace_id are irrelevant to ordering/limit and are pinned so no
# filter excludes any record; the returned count is governed purely by the limit.
_LEVEL = "INFO"
_LOGGER = "rag_system.test"
_TRACE_ID = "0" * 32


@st.composite
def _log_records(draw: st.DrawFn) -> list[LogRecordModel]:
    """Build a list of 0..150 log records.

    The upper bound (150) comfortably exceeds the default limit (100) and the
    small supplied limits under test, so truncation is a frequently exercised
    path while staying well under the 1000 maximum. ``insertion_seq`` is left at
    its default and assigned by the store on commit in persist order (R15.2);
    the message simply carries the persist index for readability.
    """
    count = draw(st.integers(min_value=0, max_value=150))
    return [
        LogRecordModel(
            timestamp=draw(_ts_pool),
            level=_LEVEL,
            logger=_LOGGER,
            message=f"record {index}",
            trace_id=_TRACE_ID,
            exc_text=None,
            extra={},
        )
        for index in range(count)
    ]


# A limit is either omitted (None -> the store applies its default of 100, R16.5)
# or a valid client-supplied value in 1..1000 (R16.6). Boundary values 1 and 1000
# are included so the smallest and largest valid limits are exercised.
_maybe_limit = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=_MAX_LIMIT),
)


def _effective_limit(limit: int | None) -> int:
    """Independent oracle for the limit actually applied.

    Restates R16.5/R16.6: an omitted limit defaults to 100; a supplied limit (in
    the valid 1..1000 range) is honoured as-is.
    """
    if limit is None:
        return _DEFAULT_LIMIT
    return min(max(0, limit), _MAX_LIMIT)


# ---------------------------------------------------------------------------
# Property 15 - log search ordering and limit are honoured.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 15: Log search ordering and limit are honoured
# Validates: Requirements 16.5, 16.6
@settings(max_examples=100)
@given(records=_log_records(), limit=_maybe_limit)
def test_log_search_ordering_and_limit_are_honoured(
    records: list[LogRecordModel], limit: int | None
) -> None:
    """``search_logs`` returns at most the effective limit of records ordered by
    timestamp descending.

    Covers the default-limit case (omitted limit -> at most 100, R16.5) and the
    supplied-limit case (1..1000 -> at most that many, R16.6). With no filters
    every stored record is a candidate, so the returned count must equal
    ``min(total, effective_limit)``; the results must be ordered by timestamp
    descending (ties broken by insertion order descending, R15.2); and the
    returned records must be the top slice of that ordering.
    """
    store = InMemoryTransactionalStore()
    for record in records:
        store.persist_log(record)

    # Build filters either omitting the limit (exercise the default) or supplying
    # a valid one, leaving all other filters unset so nothing is filtered out.
    if limit is None:
        filters = LogSearchFilters()
    else:
        filters = LogSearchFilters(limit=limit)

    results = store.search_logs(filters)

    effective = _effective_limit(limit)

    # R16.5/R16.6: never more than the effective limit.
    assert len(results) <= effective
    # With no filtering, exactly min(total, limit) records come back.
    assert len(results) == min(len(records), effective)

    # Ordered by (timestamp, insertion_seq) in descending order. The store
    # assigns insertion_seq in persist (commit) order, so it is the R15.2
    # tiebreaker that disambiguates equal timestamps.
    order_keys = [(r.timestamp, r.insertion_seq) for r in results]
    assert order_keys == sorted(order_keys, reverse=True)

    # Timestamps alone are non-increasing (the primary descending sort key).
    timestamps = [r.timestamp for r in results]
    assert timestamps == sorted(timestamps, reverse=True)

    # No record is returned more than once (insertion_seq is unique per row).
    returned_seqs = {r.insertion_seq for r in results}
    assert len(returned_seqs) == len(results)

    # The returned records are the top slice of the full descending order: the
    # smallest returned key is >= the largest excluded key. Comparing against the
    # full result set (limit = max, total <= 150 < 1000) confirms the limit
    # truncates the *tail* of the descending order, not an arbitrary subset.
    full = store.search_logs(LogSearchFilters(limit=_MAX_LIMIT))
    expected_seqs = [r.insertion_seq for r in full[:effective]]
    assert [r.insertion_seq for r in results] == expected_seqs


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
