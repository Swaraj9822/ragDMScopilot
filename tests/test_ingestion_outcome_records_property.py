"""Property test for ingestion outcome version/event records (R5.1-R5.3).

# Feature: rag-trust-and-observability, Property 15: Ingestion outcome determines version and event records

This generalizes the example-based cases in ``test_service_version_control.py``
into a single property over arbitrary version tokens and content: for any
document that already has an active version, a *second* ingestion attempt lands
in exactly one of two outcomes, and the version-control artifacts are fully
determined by that outcome.

Successful ingestion (R5.1, R5.2):
  * creates an immutable ``DocumentVersion`` manifest for the ingested version,
  * records exactly one succeeded ``IngestionEvent`` for that version, and
  * publishes the version as the document's active version (with prior versions
    retained in the index).

Failed ingestion (R5.3):
  * creates no ``DocumentVersion`` manifest for the failed version,
  * leaves the active version pointer unchanged, and
  * records a failed ``IngestionEvent`` for that version.

The CAS-capable store double pattern is reused from
``test_service_version_control.py`` so the version-index compare-and-set path is
genuinely exercised.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.models import (
    DocumentStatus,
    DocumentVersion,
)
from rag_system.storage import (
    document_record_key,
    document_version_key,
)

# Reuse the CAS store double + fakes + helpers from the example-based suite.
from test_service_version_control import (
    CasStore,
    VersionedFakeIndex,
    _events,
    _index_of,
    _record,
    _service,
)

# ---------------------------------------------------------------------------
# Generators: version tokens are the content-hash-like identifiers the service
# stamps onto a record; we only need them to be non-empty, path-safe, and
# distinct between the prior and the second ingestion.
# ---------------------------------------------------------------------------

_version_tokens = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=12,
)


@settings(deadline=None, max_examples=60)
@given(
    prior_version=_version_tokens,
    new_version=_version_tokens,
    prior_content=st.binary(min_size=1, max_size=64),
    new_content=st.binary(min_size=1, max_size=64),
    should_fail=st.booleans(),
)
def test_ingestion_outcome_determines_version_and_event_records(
    prior_version: str,
    new_version: str,
    prior_content: bytes,
    new_content: bytes,
    should_fail: bool,
) -> None:
    # The two ingestions must target distinct versions; a replacement upload
    # always stamps a fresh content-hash version.
    if prior_version == new_version:
        new_version = new_version + "x"

    store = CasStore()
    store.put_json(
        document_record_key("doc-1"),
        _record(prior_version, DocumentStatus.queued).model_dump(mode="json"),
    )
    service = _service(store, VersionedFakeIndex())

    # --- Establish a published prior active version. ---
    asyncio.run(
        service._run_ingestion(
            _record(prior_version, DocumentStatus.queued), prior_content
        )
    )
    assert _index_of(store).active_version == prior_version

    # --- Second ingestion: a replacement upload for ``new_version``. ---
    store.put_json(
        document_record_key("doc-1"),
        _record(
            new_version, DocumentStatus.queued, active_version=prior_version
        ).model_dump(mode="json"),
    )
    second_record = _record(
        new_version, DocumentStatus.queued, active_version=prior_version
    )

    if should_fail:
        # Fail on the embedding-manifest write, which runs after the vectors are
        # upserted but before the version-control artifacts are recorded.
        store.fail_put_json_when = (
            lambda k, p: isinstance(p, dict) and "chunk_count" in p
        )
        with pytest.raises(RuntimeError):
            asyncio.run(service._run_ingestion(second_record, new_content))

        # R5.3: no manifest for the failed version.
        assert store.get_json(document_version_key("doc-1", new_version)) is None

        # R5.3: the active version pointer is unchanged.
        index = _index_of(store)
        assert index.active_version == prior_version
        assert [v.version for v in index.versions] == [prior_version]

        # R5.3: a failed Ingestion_Event was recorded for the new version, and
        # the prior succeeded event is retained.
        events_by_version = {(e.version, e.status) for e in _events(store)}
        assert (new_version, "failed") in events_by_version
        assert (prior_version, "succeeded") in events_by_version
        failed = [
            e for e in _events(store) if e.version == new_version and e.status == "failed"
        ]
        assert len(failed) == 1
        assert failed[0].error
    else:
        result = asyncio.run(service._run_ingestion(second_record, new_content))
        assert result.status == DocumentStatus.indexed

        # R5.1: an immutable Document_Version manifest exists for the version.
        manifest_payload = store.get_json(document_version_key("doc-1", new_version))
        assert manifest_payload is not None
        manifest = DocumentVersion.model_validate(manifest_payload)
        assert manifest.version == new_version
        assert manifest.indexed is True
        assert manifest.vectors_present is True
        assert manifest.source_retained is True

        # R5.1: exactly one succeeded Ingestion_Event for the new version.
        succeeded_new = [
            e
            for e in _events(store)
            if e.version == new_version and e.status == "succeeded"
        ]
        assert len(succeeded_new) == 1
        assert succeeded_new[0].error is None

        # R5.2/R5.4: the version is now active, with the prior version retained.
        index = _index_of(store)
        assert index.active_version == new_version
        assert [v.version for v in index.versions] == [prior_version, new_version]
