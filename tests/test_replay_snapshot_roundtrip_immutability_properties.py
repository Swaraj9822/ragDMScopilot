"""Round-trip and immutability property tests for corpus snapshots (R8.1).

# Feature: rag-trust-and-observability, task 14.2: snapshot round-trip and immutability

Two properties over arbitrary version manifests exercise the immutability
guarantee a reproducible :class:`~rag_system.models.CorpusSnapshot` rests on
(task 14.1):

Round-trip (R8.1):
  Serializing a created snapshot and deserializing it back preserves the
  manifest (and the id/timestamp) exactly, across both the Python ``model_dump``
  representation and the JSON string representation. The JSON round-trip is the
  interesting one: ``manifest`` is a ``list[tuple[str, str]]``, so the pairs
  become JSON arrays on the wire and must coerce back to equal ``(document_id,
  document_version)`` tuples on read.

Immutability (R8.1):
  A snapshot is written create-only, so a *second* create-only write to the same
  snapshot key raises :class:`~rag_system.storage.PreconditionFailed`. Once
  captured, a snapshot cannot be mutated — a later ingestion or restore cannot
  change what a replay retrieves against.

The fake create-only store double is reused from ``test_replay_snapshots.py`` so
the create-only precondition path is genuinely exercised.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.models import CorpusSnapshot
from rag_system.replay import ReplaySnapshotStore
from rag_system.storage import PreconditionFailed, corpus_snapshot_key

# Reuse the create-only store double from the example-based suite.
from test_replay_snapshots import _FakeStore

# ---------------------------------------------------------------------------
# Generators: a manifest is a list of (document_id, document_version) pairs.
# Ids/versions only need to be arbitrary strings; the store keys on the minted
# snapshot id, not on manifest contents.
# ---------------------------------------------------------------------------

_tokens = st.text(min_size=0, max_size=24)
_manifests = st.lists(st.tuples(_tokens, _tokens), min_size=0, max_size=20)


@settings(max_examples=200)
@given(manifest=_manifests)
def test_snapshot_survives_serialize_deserialize_round_trip(
    manifest: list[tuple[str, str]],
) -> None:
    """serialize -> deserialize preserves the manifest across all manifests (R8.1)."""
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    snapshot = snapshots.create_snapshot(manifest)

    # Python-object round-trip (tuples preserved as-is).
    from_dump = CorpusSnapshot.model_validate(snapshot.model_dump())
    assert from_dump == snapshot
    assert from_dump.manifest == [(d, v) for d, v in manifest]

    # JSON-string round-trip: pairs serialize to arrays and must coerce back to
    # equal (document_id, document_version) tuples.
    from_json = CorpusSnapshot.model_validate_json(snapshot.model_dump_json())
    assert from_json == snapshot
    assert from_json.corpus_snapshot_id == snapshot.corpus_snapshot_id
    assert from_json.created_at == snapshot.created_at
    assert from_json.manifest == [(d, v) for d, v in manifest]


@settings(max_examples=200)
@given(manifest=_manifests, second=_manifests)
def test_created_snapshot_cannot_be_mutated(
    manifest: list[tuple[str, str]],
    second: list[tuple[str, str]],
) -> None:
    """A second create-only write to the same snapshot key is rejected (R8.1)."""
    store = _FakeStore()
    snapshots = ReplaySnapshotStore(store)

    snapshot = snapshots.create_snapshot(manifest, corpus_snapshot_id="fixed-id")
    key = corpus_snapshot_key(snapshot.corpus_snapshot_id)
    assert key in store.writes

    # Re-creating under the same id (even with different contents) must fail its
    # create-only precondition, leaving the originally persisted payload intact.
    with pytest.raises(PreconditionFailed):
        snapshots.create_snapshot(second, corpus_snapshot_id="fixed-id")

    persisted = CorpusSnapshot.model_validate(store.writes[key])
    assert persisted.manifest == [(d, v) for d, v in manifest]
