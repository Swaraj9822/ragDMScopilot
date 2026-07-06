"""Unit tests for replay corpus snapshots and SQL result fixtures (R8.1, R8.6).

Covers the SQL normalization/hashing helpers and the create-only persistence +
returned shape of ``ReplaySnapshotStore``. The immutability *property* (a second
create-only write to the same key fails) is checked here with a small example;
the exhaustive round-trip/immutability property test lives in task 14.2.
"""

from __future__ import annotations

import pytest

from rag_system.models import CorpusSnapshot, SqlResultFixture
from rag_system.replay import (
    ReplaySnapshotStore,
    normalize_sql,
    normalized_sql_hash,
)
from rag_system.storage import (
    PreconditionFailed,
    corpus_snapshot_key,
    sql_result_fixture_key,
)


class _FakeStore:
    """Create-only store that rejects a duplicate key with ``PreconditionFailed``.

    Mirrors :meth:`GcsArtifactStore.create_json` semantics: the first write to a
    key succeeds, a second write to the same key fails its precondition.
    """

    def __init__(self) -> None:
        self.writes: dict[str, object] = {}

    def create_json(self, key: str, payload: object) -> str:
        if key in self.writes:
            raise PreconditionFailed(key)
        self.writes[key] = payload
        return f"s3://fake/{key}"


# ---------------------------------------------------------------------------
# SQL normalization + hashing (R8.6)
# ---------------------------------------------------------------------------


def test_normalize_sql_folds_whitespace_case_and_trailing_semicolon() -> None:
    assert (
        normalize_sql("  SELECT   *\n  FROM Orders ;  ")
        == "select * from orders"
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("SELECT * FROM t", "select * from t"),
        ("SELECT * FROM t", "SELECT  *  FROM  t"),
        ("SELECT * FROM t", "SELECT * FROM t;"),
        ("SELECT * FROM t", "select * from t ;\n"),
    ],
)
def test_normalized_sql_hash_stable_across_equivalent_sql(a: str, b: str) -> None:
    assert normalized_sql_hash(a) == normalized_sql_hash(b)


def test_normalized_sql_hash_differs_for_different_queries() -> None:
    assert normalized_sql_hash("SELECT * FROM a") != normalized_sql_hash(
        "SELECT * FROM b"
    )


# ---------------------------------------------------------------------------
# CorpusSnapshot creation (R8.1)
# ---------------------------------------------------------------------------


def test_create_snapshot_persists_create_only_and_returns_manifest() -> None:
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    snapshot = snapshots.create_snapshot([("doc-a", "v1"), ("doc-b", "v3")])

    key = corpus_snapshot_key(snapshot.corpus_snapshot_id)
    assert key in store.writes
    persisted = CorpusSnapshot.model_validate(store.writes[key])

    assert persisted.corpus_snapshot_id == snapshot.corpus_snapshot_id
    assert persisted.manifest == [("doc-a", "v1"), ("doc-b", "v3")]
    assert persisted.created_at == snapshot.created_at


def test_create_snapshot_mints_unique_ids() -> None:
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    ids = {snapshots.create_snapshot([]).corpus_snapshot_id for _ in range(10)}
    assert len(ids) == 10


def test_create_snapshot_is_immutable_on_id_collision() -> None:
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    snapshots.create_snapshot([("doc-a", "v1")], corpus_snapshot_id="fixed")
    with pytest.raises(PreconditionFailed):
        snapshots.create_snapshot([("doc-a", "v2")], corpus_snapshot_id="fixed")


# ---------------------------------------------------------------------------
# SqlResultFixture creation (R8.6)
# ---------------------------------------------------------------------------


def test_create_sql_fixture_keys_on_snapshot_and_normalized_hash() -> None:
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    fixture = snapshots.create_sql_fixture(
        corpus_snapshot_id="snap-1",
        sql="SELECT * FROM orders;",
        rows=[{"id": 1}, {"id": 2}],
    )

    expected_hash = normalized_sql_hash("SELECT * FROM orders;")
    assert fixture.fixture_id == expected_hash
    assert fixture.normalized_sql_hash == expected_hash

    key = sql_result_fixture_key("snap-1", expected_hash)
    assert key in store.writes
    persisted = SqlResultFixture.model_validate(store.writes[key])
    assert persisted.corpus_snapshot_id == "snap-1"
    assert persisted.sql == "SELECT * FROM orders;"
    assert persisted.rows == [{"id": 1}, {"id": 2}]


def test_equivalent_sql_maps_to_same_fixture_key_and_is_immutable() -> None:
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    snapshots.create_sql_fixture(
        corpus_snapshot_id="snap-1",
        sql="SELECT * FROM orders",
        rows=[{"id": 1}],
    )
    # Normalization-equivalent SQL resolves to the same key → create-only reject.
    with pytest.raises(PreconditionFailed):
        snapshots.create_sql_fixture(
            corpus_snapshot_id="snap-1",
            sql="select   *   from orders ;",
            rows=[{"id": 99}],
        )


def test_same_sql_under_different_snapshots_are_distinct_fixtures() -> None:
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    f1 = snapshots.create_sql_fixture(
        corpus_snapshot_id="snap-1", sql="SELECT 1", rows=[]
    )
    f2 = snapshots.create_sql_fixture(
        corpus_snapshot_id="snap-2", sql="SELECT 1", rows=[]
    )

    assert f1.normalized_sql_hash == f2.normalized_sql_hash
    assert sql_result_fixture_key("snap-1", f1.fixture_id) in store.writes
    assert sql_result_fixture_key("snap-2", f2.fixture_id) in store.writes
