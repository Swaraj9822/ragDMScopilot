"""Property test for log retrieval by trace id.

Feature: ai-observability-platform.

This module exercises log retrieval through the in-memory transactional store
double (``tests/observability_tracing_store_double.py``), the same double the
spec designates for the persistence/query property tests (task 8.2). It isolates
the *retrieve-by-trace-id* behaviour of ``get_logs_by_trace`` (design Property 17
/ R15.1, R15.2):

- fetching by a syntactically valid trace_id returns *exactly* the records whose
  trace_id equals that value (R15.1), and
- those records are ordered by record timestamp descending, ties broken by
  descending insertion order (R15.2).

Generators are constrained to the retrieval domain:

- ``trace_id`` is drawn from a small pool of distinct 32-char lowercase hex
  values plus ``None`` (the explicit-null case, R14.3) so that the store holds a
  mix of matching, non-matching, and null-trace records and the equality filter
  is exercised against real collisions and near-misses;
- record timestamps are drawn from a small shared pool so equal timestamps are
  common, forcing the descending-by-timestamp ordering to be checked in the
  presence of ties where the insertion-order tiebreaker (R15.2) decides order;
- ``insertion_seq`` is left at its default and assigned by the store on commit
  in persist order, exactly as the real Log_Store will (R15.2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.models import LogRecordModel

# Test-support module (not collected by pytest); imported by module name, the
# repo's convention for shared test fakes (see test_preservation_properties.py).
from observability_tracing_store_double import InMemoryTransactionalStore

# ---------------------------------------------------------------------------
# Smart generators - constrained to the retrieval domain.
# ---------------------------------------------------------------------------

# A small pool of distinct 32-char lowercase hex trace ids. Keeping the pool
# small guarantees frequent collisions, so a query trace_id routinely matches
# several stored records and is contrasted against records carrying a different
# trace_id (or no trace_id at all).
_TRACE_IDS = (
    "0" * 32,
    "a" * 32,
    "1234567890abcdef" * 2,
    "fedcba9876543210" * 2,
)

# A record's trace_id is either one of the pooled values or None (the explicit
# null case, R14.3). Null-trace records must never be returned for a valid
# trace_id query, so this exercises the exact-equality filter.
_record_trace_ids = st.one_of(
    st.none(),
    st.sampled_from(_TRACE_IDS),
)

# Query trace_id is always one of the pooled (valid) values, so empty and
# non-empty result sets are both exercised depending on what was stored.
_query_trace_ids = st.sampled_from(_TRACE_IDS)

# A small UTC timestamp pool shared by all records so equal timestamps are
# common; this forces the descending-by-timestamp ordering (R15.2) to be
# exercised in the presence of ties, where the insertion-order tiebreaker
# decides the relative order.
_BASE_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TS_OFFSETS = (0, 5, 10, 15, 20)
_ts_pool = st.sampled_from([_BASE_TS + timedelta(seconds=o) for o in _TS_OFFSETS])

_LEVEL = "INFO"
_LOGGER = "rag_system.test"


@st.composite
def _log_records(draw: st.DrawFn) -> list[LogRecordModel]:
    """Build a list of 0..60 log records with mixed trace_ids and tied timestamps.

    ``insertion_seq`` is left at its default and assigned by the store on commit
    in persist order (R15.2); the message carries the persist index purely for
    readability.
    """
    count = draw(st.integers(min_value=0, max_value=60))
    return [
        LogRecordModel(
            timestamp=draw(_ts_pool),
            level=_LEVEL,
            logger=_LOGGER,
            message=f"record {index}",
            trace_id=draw(_record_trace_ids),
            exc_text=None,
            extra={},
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# Property 17 - log retrieval by trace id returns all matching records in
# tie-broken order.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 17: Log retrieval by trace id returns all matching records in tie-broken order
# Validates: Requirements 15.1, 15.2
@settings(max_examples=100)
@given(records=_log_records(), query=_query_trace_ids)
def test_log_retrieval_by_trace_id_returns_all_matching_in_order(
    records: list[LogRecordModel], query: str
) -> None:
    """``get_logs_by_trace`` returns exactly the matching records, ordered desc.

    For any set of stored Log_Records, fetching by a syntactically valid
    trace_id returns exactly the records whose trace_id equals that value
    (R15.1), ordered by timestamp descending with ties broken by descending
    insertion order (R15.2).
    """
    store = InMemoryTransactionalStore()
    for record in records:
        store.persist_log(record)

    results = store.get_logs_by_trace(query)

    # R15.1: every returned record matches the query trace_id exactly, and no
    # matching record is omitted. The store assigns a unique insertion_seq per
    # committed row, so identity-by-seq gives an exact set comparison. A record
    # carrying trace_id=None must never match a valid query.
    returned_seqs = [r.insertion_seq for r in results]
    assert all(r.trace_id == query for r in results)
    assert sorted(returned_seqs) == sorted(
        idx + 1 for idx, rec in enumerate(records) if rec.trace_id == query
    )

    # No record is returned more than once (insertion_seq is unique per row).
    assert len(set(returned_seqs)) == len(returned_seqs)

    # R15.2: ordered by (timestamp, insertion_seq) in descending order. With the
    # store assigning insertion_seq in commit order, this is the tiebreaker that
    # disambiguates the frequently-tied timestamps.
    order_keys = [(r.timestamp, r.insertion_seq) for r in results]
    assert order_keys == sorted(order_keys, reverse=True)

    # Timestamps alone are non-increasing (the primary descending sort key).
    timestamps = [r.timestamp for r in results]
    assert timestamps == sorted(timestamps, reverse=True)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
