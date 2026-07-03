"""Unit tests for the GCS-backed artifact store.

The store is constructed via ``object.__new__`` with a fake bucket/client, so no
real GCS client or credentials are needed. These cover the migration-critical
behaviours: conditional writes mapping to GCS *generation* preconditions,
translation of GCS ``PreconditionFailed`` into the store's own exception, and
NotFound-tolerant reads / prefix listing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gcloud_exceptions

from rag_system import storage
from rag_system.storage import GcsArtifactStore, PreconditionFailed


class _Recorder:
    def __init__(self) -> None:
        self.uploads: list[SimpleNamespace] = []
        self.raise_precondition = False
        self.transient_remaining = 0


class _FakeBlob:
    def __init__(self, recorder: _Recorder, key: str, existing: bytes | None) -> None:
        self._rec = recorder
        self._key = key
        self._existing = existing
        self.kms_key_name = None
        self.generation = 42

    def upload_from_string(self, content, content_type, if_generation_match=None):
        self._rec.uploads.append(
            SimpleNamespace(
                key=self._key,
                content=content,
                content_type=content_type,
                if_generation_match=if_generation_match,
                kms_key_name=self.kms_key_name,
            )
        )
        if self._rec.transient_remaining > 0:
            self._rec.transient_remaining -= 1
            raise RuntimeError("transient network blip")
        if self._rec.raise_precondition:
            raise gcloud_exceptions.PreconditionFailed("generation mismatch")

    def download_as_bytes(self):
        if self._existing is None:
            raise gcloud_exceptions.NotFound("missing")
        return self._existing


class _FakeBucket:
    def __init__(self, recorder: _Recorder, blobs: dict[str, bytes]) -> None:
        self._rec = recorder
        self._blobs = blobs

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self._rec, key, self._blobs.get(key))

    def get_blob(self, key: str) -> _FakeBlob | None:
        if key not in self._blobs:
            return None
        return _FakeBlob(self._rec, key, self._blobs[key])


class _FakeClient:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_blobs(self, bucket_or_name, prefix=None):
        for name in self._names:
            if prefix is None or name.startswith(prefix):
                yield SimpleNamespace(name=name)


def _store(
    blobs: dict[str, bytes] | None = None,
    names: list[str] | None = None,
    *,
    kms: str | None = None,
) -> tuple[GcsArtifactStore, _Recorder]:
    rec = _Recorder()
    store = object.__new__(GcsArtifactStore)
    store._bucket = "b"
    store._kms_key_name = kms
    store._bucket_obj = _FakeBucket(rec, blobs or {})
    store._client = _FakeClient(names or [])
    return store, rec


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    # _put_bytes_conditional is wrapped with tenacity; silence its back-off so a
    # deterministic PreconditionFailed does not sleep through retries.
    for method in (
        GcsArtifactStore._put_bytes_conditional,
        GcsArtifactStore.get_json,
        GcsArtifactStore.get_json_with_etag,
    ):
        monkeypatch.setattr(method.retry, "sleep", lambda *a, **k: None)


def test_create_json_uses_generation_zero_precondition() -> None:
    store, rec = _store()
    store.create_json("documents/d/record.json", {"a": 1})
    upload = rec.uploads[-1]
    assert upload.if_generation_match == 0
    assert upload.content_type == "application/json"
    assert b'"a": 1' in upload.content


def test_if_match_maps_to_generation_number() -> None:
    store, rec = _store()
    store.put_json_conditional("k", {"x": 2}, if_match="7")
    assert rec.uploads[-1].if_generation_match == 7


def test_precondition_failure_is_translated_and_not_retried() -> None:
    store, rec = _store()
    rec.raise_precondition = True
    with pytest.raises(PreconditionFailed):
        store.create_json("k", {"a": 1})
    # A 412 is deterministic: it must propagate on the first attempt, not burn
    # the transient-retry budget.
    assert len(rec.uploads) == 1


def test_transient_error_is_still_retried_then_succeeds() -> None:
    store, rec = _store()
    rec.transient_remaining = 2  # fail twice, succeed on the third attempt
    store.create_json("k", {"a": 1})
    assert len(rec.uploads) == 3


def test_kms_key_is_applied_to_uploads() -> None:
    store, rec = _store(kms="projects/p/locations/l/keyRings/r/cryptoKeys/k")
    store.put_json("k", {"a": 1})
    assert rec.uploads[-1].kms_key_name == "projects/p/locations/l/keyRings/r/cryptoKeys/k"


def test_get_json_returns_none_when_missing() -> None:
    store, _ = _store(blobs={})
    assert store.get_json("missing") is None


def test_get_json_parses_existing_object() -> None:
    store, _ = _store(blobs={"k": b'{"hello": "world"}'})
    assert store.get_json("k") == {"hello": "world"}


def test_get_json_with_etag_returns_generation_string() -> None:
    store, _ = _store(blobs={"k": b'{"v": 1}'})
    payload, etag = store.get_json_with_etag("k")
    assert payload == {"v": 1}
    assert etag == "42"


def test_get_json_with_etag_missing_is_none_none() -> None:
    store, _ = _store(blobs={})
    assert store.get_json_with_etag("missing") == (None, None)


def test_list_document_record_keys_filters_records() -> None:
    names = [
        "documents/a/record.json",
        "documents/a/versions/v1.json",
        "documents/b/record.json",
        "other/thing.json",
    ]
    store, _ = _store(names=names)
    assert store.list_document_record_keys() == [
        "documents/a/record.json",
        "documents/b/record.json",
    ]


def test_list_query_trace_keys_filters_traces() -> None:
    names = [
        "queries/t1/trace.json",
        "queries/t1/feedback/f1.json",
        "queries/t2/trace.json",
        "documents/a/record.json",
    ]
    store, _ = _store(names=names)
    assert store.list_query_trace_keys() == [
        "queries/t1/trace.json",
        "queries/t2/trace.json",
    ]


def test_returned_uri_uses_gs_scheme() -> None:
    store, _ = _store()
    assert storage.__name__  # module import sanity
    assert store.put_json("k", {"a": 1}) == "gs://b/k"
