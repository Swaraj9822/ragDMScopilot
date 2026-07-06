import pytest
from hypothesis import given
from hypothesis import strategies as st

from rag_system.storage import (
    PreconditionFailed,
    ai_config_index_key,
    ai_config_version_key,
    chunks_key,
    clarification_key,
    corpus_snapshot_key,
    document_record_key,
    document_version_index_key,
    document_version_key,
    embedding_manifest_key,
    evaluation_run_results_key,
    evaluation_set_case_key,
    feedback_index_key,
    ingestion_event_key,
    knowledge_gap_map_key,
    parsed_key,
    query_feedback_key,
    query_trace_key,
    raw_pdf_key,
    replay_run_key,
    sql_result_fixture_key,
)


def test_s3_keys_are_stable() -> None:
    document_id = "doc-123"
    version = "abc"

    assert raw_pdf_key(document_id, version) == "raw/doc-123/abc/source.pdf"
    assert parsed_key(document_id, version) == "parsed/doc-123/abc/llamaparse.json"
    assert chunks_key(document_id, version) == "chunks/doc-123/abc/chunks.jsonl"
    assert embedding_manifest_key(document_id, version) == "embeddings/doc-123/abc/manifest.json"
    assert document_record_key(document_id) == "documents/doc-123/record.json"
    assert query_trace_key("trace-123") == "queries/trace-123/trace.json"
    assert query_feedback_key("trace-123", "feedback-1") == (
        "queries/trace-123/feedback/feedback-1.json"
    )


def test_new_artifact_keys_are_stable() -> None:
    """Key patterns match the persistence layout table in the design document."""
    assert document_version_key("doc-1", "v2") == "documents/doc-1/versions/v2.json"
    assert document_version_index_key("doc-1") == "documents/doc-1/versions/index.json"
    assert ingestion_event_key("doc-1", "ing-9") == "documents/doc-1/ingestions/ing-9.json"
    assert clarification_key("clar-7") == "clarifications/clar-7.json"
    assert feedback_index_key() == "feedback_index/negative.jsonl"
    assert evaluation_set_case_key("set-1", "case-3") == "evaluation/sets/set-1/cases/case-3.json"
    assert evaluation_run_results_key("run-5") == "evaluation/runs/run-5/results.json"
    assert ai_config_version_key("cfg-1", "ver-2") == "ai_config/cfg-1/versions/ver-2.json"
    assert ai_config_index_key("cfg-1") == "ai_config/cfg-1/index.json"
    assert corpus_snapshot_key("snap-1") == "corpus_snapshots/snap-1.json"
    assert sql_result_fixture_key("snap-1", "fix-2") == "corpus_snapshots/snap-1/sql/fix-2.json"
    assert replay_run_key("replay-8") == "replays/replay-8.json"
    assert knowledge_gap_map_key("2024-01-01T00:00:00Z") == "knowledge_gap/2024-01-01T00:00:00Z.json"


# --- Key-function properties -------------------------------------------------

_ID = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122, blacklist_characters="/"),
    min_size=1,
    max_size=40,
)

_KEY_FUNCS_SINGLE_ARG = [
    document_version_index_key,
    clarification_key,
    evaluation_run_results_key,
    ai_config_index_key,
    corpus_snapshot_key,
    replay_run_key,
    knowledge_gap_map_key,
]

_KEY_FUNCS_TWO_ARG = [
    document_version_key,
    ingestion_event_key,
    evaluation_set_case_key,
    ai_config_version_key,
    sql_result_fixture_key,
]


@given(arg=_ID)
def test_single_arg_keys_embed_id_as_a_path_segment(arg: str) -> None:
    for func in _KEY_FUNCS_SINGLE_ARG:
        key = func(arg)
        assert key.endswith(".json")
        assert not key.startswith("/")
        segments = key.split("/")
        # The id is either its own directory segment or the filename stem.
        assert arg in segments or f"{arg}.json" in segments


@given(a=_ID, b=_ID)
def test_two_arg_keys_scope_second_id_under_first(a: str, b: str) -> None:
    for func in _KEY_FUNCS_TWO_ARG:
        key = func(a, b)
        assert key.endswith(".json")
        segments = key.split("/")
        # First id names a directory segment (the parent scope); the second id
        # is the filename stem of the artifact under it.
        assert a in segments
        assert segments[-1] == f"{b}.json"


# --- Write-helper doubles ----------------------------------------------------


class _FakeStore:
    """Minimal in-memory stand-in exposing the CAS primitives used by the helpers.

    Mirrors the compare-and-set doubles already used in the service tests, so we
    can exercise ``create_json`` / ``update_json_cas`` without real S3.
    """

    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        self._bucket = "test-bucket"

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    def get_json_with_etag(self, key: str) -> tuple[object | None, str | None]:
        entry = self.objects.get(key)
        if entry is None:
            return None, None
        return entry[0], entry[1]

    def put_json_conditional(
        self,
        key: str,
        payload: object,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        entry = self.objects.get(key)
        if if_none_match:
            if entry is not None:
                raise PreconditionFailed(key)
        elif if_match is not None:
            if entry is None or entry[1] != if_match:
                raise PreconditionFailed(key)
        self.objects[key] = (payload, self._next_etag())

    # Bind the real, store-agnostic helper implementations.
    from rag_system.storage import GcsArtifactStore

    create_json = GcsArtifactStore.create_json
    update_json_cas = GcsArtifactStore.update_json_cas


def test_create_json_writes_when_absent_and_rejects_second_write() -> None:
    store = _FakeStore()
    key = document_version_key("doc-1", "v1")

    store.create_json(key, {"version": "v1"})
    assert store.objects[key][0] == {"version": "v1"}

    # Immutability: a second create-only write to the same key must fail.
    with pytest.raises(PreconditionFailed):
        store.create_json(key, {"version": "v1-tampered"})
    assert store.objects[key][0] == {"version": "v1"}


def test_update_json_cas_creates_then_updates() -> None:
    store = _FakeStore()
    key = document_version_index_key("doc-1")

    result = store.update_json_cas(key, lambda cur: {"versions": ["v1"], "active": "v1"})
    assert result == {"versions": ["v1"], "active": "v1"}

    def append_v2(current: object | None) -> object:
        assert isinstance(current, dict)
        versions = list(current["versions"]) + ["v2"]
        return {"versions": versions, "active": "v2"}

    result = store.update_json_cas(key, append_v2)
    assert result == {"versions": ["v1", "v2"], "active": "v2"}
    assert store.objects[key][0] == {"versions": ["v1", "v2"], "active": "v2"}


def test_update_json_cas_retries_on_concurrent_write() -> None:
    store = _FakeStore()
    key = replay_run_key("replay-1")
    store.update_json_cas(key, lambda cur: {"state": "queued"})

    calls = {"n": 0}
    original_put = store.put_json_conditional

    def racing_put(k: str, payload: object, **kwargs: object) -> None:
        # Simulate one concurrent writer bumping the ETag before our first
        # write lands, forcing exactly one CAS retry.
        if calls["n"] == 0:
            calls["n"] += 1
            store.objects[k] = (store.objects[k][0], store._next_etag())
        original_put(k, payload, **kwargs)  # type: ignore[arg-type]

    store.put_json_conditional = racing_put  # type: ignore[assignment]

    result = store.update_json_cas(key, lambda cur: {"state": "running"})
    assert result == {"state": "running"}
    assert store.objects[key][0] == {"state": "running"}


def test_update_json_cas_gives_up_after_max_attempts() -> None:
    store = _FakeStore()
    key = replay_run_key("replay-2")
    store.update_json_cas(key, lambda cur: {"state": "queued"})

    def always_conflict(k: str, payload: object, **kwargs: object) -> None:
        raise PreconditionFailed(k)

    store.put_json_conditional = always_conflict  # type: ignore[assignment]

    with pytest.raises(PreconditionFailed):
        store.update_json_cas(key, lambda cur: {"state": "running"}, max_attempts=3)
