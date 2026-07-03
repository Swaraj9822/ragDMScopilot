"""Property test for document version invariants across operation sequences.

# Feature: rag-trust-and-observability, Property 16: Document version invariants hold across operation sequences

This raises the single-step Property 15 (``test_ingestion_outcome_records_property.py``)
to a *sequence-level* invariant. For any sequence of ingestion operations on a
Document — each either a successful ingestion of a fresh version or a failed
ingestion — the following invariants must hold *after every step*:

* **At most one Active_Version at all times** (R5.4): the version index carries a
  single active pointer, and when set it names exactly one version that is
  present (and indexed) in the version list.
* **Exactly one Active_Version for a non-deleted Document with >= 1 successfully
  indexed version** (R5.4): once any ingestion has succeeded, the active pointer
  is non-null and names the most recently succeeded version. Conversely, while
  no ingestion has succeeded, there is no version index and therefore no active
  version.
* **All version source content is retained** (R5.5): every version that has ever
  succeeded keeps its immutable ``DocumentVersion`` manifest (``source_retained``
  true) and stays listed in the version index, including superseded and
  non-active versions.

The CAS-capable store double + fakes are reused from
``test_service_version_control.py`` so the version-index compare-and-set path is
genuinely exercised across the whole operation sequence.

Note: R5's restore/delete endpoints are formalized in later tasks; this property
covers the ingestion-operation sequences implemented in task 9.1, which are the
operations that mutate the version index.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.models import (
    DocumentStatus,
    DocumentVersion,
    DocumentVersionIndex,
)
from rag_system.storage import (
    document_record_key,
    document_version_index_key,
    document_version_key,
)

# Reuse the CAS store double + fakes + helpers from the example-based suite.
from test_service_version_control import (
    CasStore,
    VersionedFakeIndex,
    _record,
    _service,
)


def _maybe_index(store: CasStore) -> DocumentVersionIndex | None:
    """Return the parsed version index, or ``None`` when it has not been created."""
    payload = store.get_json(document_version_index_key("doc-1"))
    if payload is None:
        return None
    return DocumentVersionIndex.model_validate(payload)


def _assert_invariants(
    store: CasStore, succeeded_versions: list[str]
) -> None:
    """Assert Property 16's version invariants against the current store state."""
    index = _maybe_index(store)

    if not succeeded_versions:
        # No ingestion has succeeded yet: the version index is never created by
        # a failed ingestion, so there is no active version to speak of.
        assert index is None
        return

    # A non-deleted Document with >= 1 successfully indexed version.
    assert index is not None

    # --- Exactly one Active_Version, naming the most recent success. ---
    assert index.active_version is not None
    assert index.active_version == succeeded_versions[-1]

    # --- At most one Active_Version: the active pointer names exactly one
    #     listed, indexed version. ---
    active_matches = [v for v in index.versions if v.version == index.active_version]
    assert len(active_matches) == 1
    assert active_matches[0].indexed is True

    # --- All version source content retained (including superseded/non-active).
    listed = {v.version for v in index.versions}
    # Every version that ever succeeded is still present exactly once, with its
    # immutable manifest retained and flagged as source-retained.
    assert listed == set(succeeded_versions)
    assert len(index.versions) == len(set(succeeded_versions))
    for version in set(succeeded_versions):
        manifest_payload = store.get_json(document_version_key("doc-1", version))
        assert manifest_payload is not None
        manifest = DocumentVersion.model_validate(manifest_payload)
        assert manifest.source_retained is True
        assert manifest.indexed is True


@settings(deadline=None, max_examples=60)
@given(operations=st.lists(st.booleans(), min_size=1, max_size=8))
def test_version_invariants_hold_across_operation_sequences(
    operations: list[bool],
) -> None:
    """Run a sequence of ingest/fail operations and check invariants after each.

    ``operations[i] is True`` means step ``i`` is a *successful* ingestion of a
    fresh version; ``False`` means the ingestion *fails* (its embedding-manifest
    write is rejected), which must record a failed event without creating a
    version or moving the active pointer.
    """
    store = CasStore()
    service = _service(store, VersionedFakeIndex())

    succeeded_versions: list[str] = []
    expected_active: str | None = None

    for i, succeed in enumerate(operations):
        version = f"v{i}"
        record = _record(
            version, DocumentStatus.queued, active_version=expected_active
        )
        # The record is written before the job runs, mirroring the queue step.
        store.put_json(
            document_record_key("doc-1"), record.model_dump(mode="json")
        )
        content = f"content-{i}".encode()

        if succeed:
            store.fail_put_json_when = None
            result = asyncio.run(service._run_ingestion(record, content))
            assert result.status == DocumentStatus.indexed
            succeeded_versions.append(version)
            expected_active = version
        else:
            # Fail on the embedding-manifest write (after vectors are upserted,
            # before any version-control artifact is recorded).
            store.fail_put_json_when = (
                lambda k, p: isinstance(p, dict) and "chunk_count" in p
            )
            with pytest.raises(RuntimeError):
                asyncio.run(service._run_ingestion(record, content))
            store.fail_put_json_when = None
            # A failed ingestion must not change the active version.

        _assert_invariants(store, succeeded_versions)

    # Final belt-and-suspenders check after the whole sequence.
    _assert_invariants(store, succeeded_versions)
