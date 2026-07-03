"""Unit tests for replay request validation and queued run creation (R8.1–R8.4, task 14.3).

Covers:
- Approved AI configuration version validation (R8.3)
- Retrieval parameter range checks (R8.4)
- Corpus snapshot existence validation (R8.4)
- Queued run creation on success (R8.2)
"""

from __future__ import annotations

import pytest

from rag_system.models import (
    AIConfigurationVersion,
    CorpusSnapshot,
    ReplayRetrievalParams,
    ReplayRun,
    ReplayRunRequest,
    ReplayRunState,
)
from rag_system.replay import ReplayService, ReplayValidationError
from rag_system.storage import (
    PreconditionFailed,
    ai_config_version_key,
    corpus_snapshot_key,
    replay_run_key,
)


# ---------------------------------------------------------------------------
# Fake store
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory store with create-only and CAS semantics for testing."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    def get_json(self, key: str) -> object | None:
        entry = self.objects.get(key)
        return None if entry is None else entry[0]

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

    # Bind the real helpers from the store class.
    from rag_system.storage import S3ArtifactStore

    create_json = S3ArtifactStore.create_json
    update_json_cas = S3ArtifactStore.update_json_cas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_approved_version(
    store: _FakeStore,
    config_id: str = "default",
    version_id: str = "v1",
    prompt: str = "answer the question",
    model: str = "gemini-3.5-flash",
) -> AIConfigurationVersion:
    """Store an approved AI configuration version for testing."""
    version = AIConfigurationVersion(
        config_id=config_id,
        version_id=version_id,
        prompt=prompt,
        model=model,
        output_schema={},
        router_threshold=0.5,
        retrieval_settings={},
        reranker_config={},
        change_description="initial approved config",
        created_at="2024-01-01T00:00:00+00:00",
        approved=True,
        approver="operator@example.com",
        approved_at="2024-01-01T01:00:00+00:00",
    )
    key = ai_config_version_key(config_id, version_id)
    store.objects[key] = (version.model_dump(), '"etag-seed"')
    return version


def _seed_unapproved_version(
    store: _FakeStore,
    config_id: str = "default",
    version_id: str = "v-unapproved",
) -> AIConfigurationVersion:
    """Store an un-approved AI configuration version for testing."""
    version = AIConfigurationVersion(
        config_id=config_id,
        version_id=version_id,
        prompt="test prompt",
        model="gemini-3.5-flash",
        output_schema={},
        router_threshold=0.5,
        retrieval_settings={},
        reranker_config={},
        change_description="not yet approved",
        created_at="2024-01-01T00:00:00+00:00",
        approved=False,
    )
    key = ai_config_version_key(config_id, version_id)
    store.objects[key] = (version.model_dump(), '"etag-seed"')
    return version


def _seed_snapshot(
    store: _FakeStore,
    snapshot_id: str = "snap-1",
) -> CorpusSnapshot:
    """Store a corpus snapshot for testing."""
    snapshot = CorpusSnapshot(
        corpus_snapshot_id=snapshot_id,
        created_at="2024-01-01T00:00:00+00:00",
        manifest=[("doc-a", "v1"), ("doc-b", "v2")],
    )
    key = corpus_snapshot_key(snapshot_id)
    store.objects[key] = (snapshot.model_dump(), '"etag-seed"')
    return snapshot


def _valid_request(
    version_id: str = "default:v1",
    snapshot_id: str = "snap-1",
    max_passages: int = 10,
    min_score: float = 0.5,
) -> ReplayRunRequest:
    """Build a valid replay run request for the seeded test data."""
    return ReplayRunRequest(
        question="What is the answer?",
        ai_configuration_version_id=version_id,
        retrieval_params=ReplayRetrievalParams(
            max_passages=max_passages,
            min_score=min_score,
        ),
        corpus_snapshot_id=snapshot_id,
    )


# ---------------------------------------------------------------------------
# Tests — successful queued creation (R8.2)
# ---------------------------------------------------------------------------


def test_create_replay_run_returns_queued_state() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    run = service.create_replay_run(_valid_request())

    assert run.state == ReplayRunState.queued
    assert run.replay_run_id  # non-empty id
    assert run.request.question == "What is the answer?"
    assert run.result is None
    assert run.failure_reason is None
    assert run.cancel_requested is False


def test_create_replay_run_persists_to_store() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    run = service.create_replay_run(_valid_request())

    key = replay_run_key(run.replay_run_id)
    persisted = store.get_json(key)
    assert persisted is not None
    restored = ReplayRun.model_validate(persisted)
    assert restored.replay_run_id == run.replay_run_id
    assert restored.state == ReplayRunState.queued


def test_create_replay_run_mints_unique_ids() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    ids = {
        service.create_replay_run(_valid_request()).replay_run_id
        for _ in range(10)
    }
    assert len(ids) == 10


# ---------------------------------------------------------------------------
# Tests — AI configuration version validation (R8.3)
# ---------------------------------------------------------------------------


def test_rejects_nonexistent_configuration_version() -> None:
    store = _FakeStore()
    _seed_snapshot(store)
    service = ReplayService(store)

    request = _valid_request(version_id="default:does-not-exist")
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "approved_configuration_required"
    assert "not found" in exc_info.value.detail.lower()


def test_rejects_unapproved_configuration_version() -> None:
    store = _FakeStore()
    _seed_unapproved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    request = _valid_request(version_id="default:v-unapproved")
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "approved_configuration_required"
    assert "not approved" in exc_info.value.detail.lower()


def test_rejects_when_no_colon_and_version_not_found() -> None:
    """When version_id has no colon, uses 'default' as config_id."""
    store = _FakeStore()
    _seed_snapshot(store)
    service = ReplayService(store)

    request = _valid_request(version_id="unknown-version-no-colon")
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "approved_configuration_required"


# ---------------------------------------------------------------------------
# Tests — retrieval params validation (R8.4)
# ---------------------------------------------------------------------------


def test_rejects_max_passages_below_minimum() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    # Construct request directly bypassing Pydantic validation
    request = ReplayRunRequest.model_construct(
        question="q",
        ai_configuration_version_id="default:v1",
        retrieval_params=ReplayRetrievalParams.model_construct(
            max_passages=0,
            min_score=0.5,
        ),
        corpus_snapshot_id="snap-1",
    )
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "max_passages"


def test_rejects_max_passages_above_maximum() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    request = ReplayRunRequest.model_construct(
        question="q",
        ai_configuration_version_id="default:v1",
        retrieval_params=ReplayRetrievalParams.model_construct(
            max_passages=101,
            min_score=0.5,
        ),
        corpus_snapshot_id="snap-1",
    )
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "max_passages"


def test_rejects_min_score_below_zero() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    request = ReplayRunRequest.model_construct(
        question="q",
        ai_configuration_version_id="default:v1",
        retrieval_params=ReplayRetrievalParams.model_construct(
            max_passages=10,
            min_score=-0.1,
        ),
        corpus_snapshot_id="snap-1",
    )
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "min_score"


def test_rejects_min_score_above_one() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    request = ReplayRunRequest.model_construct(
        question="q",
        ai_configuration_version_id="default:v1",
        retrieval_params=ReplayRetrievalParams.model_construct(
            max_passages=10,
            min_score=1.01,
        ),
        corpus_snapshot_id="snap-1",
    )
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "min_score"


def test_accepts_boundary_retrieval_params() -> None:
    """min(1, 0.00) and max(100, 1.00) are valid."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    service = ReplayService(store)

    # Low boundary
    run_low = service.create_replay_run(
        _valid_request(max_passages=1, min_score=0.0)
    )
    assert run_low.state == ReplayRunState.queued

    # High boundary
    run_high = service.create_replay_run(
        _valid_request(max_passages=100, min_score=1.0)
    )
    assert run_high.state == ReplayRunState.queued


# ---------------------------------------------------------------------------
# Tests — corpus snapshot validation (R8.4)
# ---------------------------------------------------------------------------


def test_rejects_nonexistent_corpus_snapshot() -> None:
    store = _FakeStore()
    _seed_approved_version(store)
    # No snapshot seeded
    service = ReplayService(store)

    request = _valid_request(snapshot_id="nonexistent-snapshot")
    with pytest.raises(ReplayValidationError) as exc_info:
        service.create_replay_run(request)

    assert exc_info.value.code == "corpus_snapshot_id"
    assert "does not exist" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Tests — compound config_id:version_id parsing
# ---------------------------------------------------------------------------


def test_compound_config_id_version_id_parsing() -> None:
    """Config ID and version ID are separated by colon."""
    store = _FakeStore()
    _seed_approved_version(store, config_id="my-config", version_id="v42")
    _seed_snapshot(store)
    service = ReplayService(store)

    request = _valid_request(version_id="my-config:v42")
    run = service.create_replay_run(request)
    assert run.state == ReplayRunState.queued
