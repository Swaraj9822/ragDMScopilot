"""Unit tests for replay worker execution and lifecycle (R8.5–R8.9, task 14.6).

Covers:
- queued → running → completed lifecycle with full result recording (R8.5, R8.7)
- Snapshot-scoped retrieval (only manifest document/version pairs)
- SQL-route fixture lookup; missing fixture → failed (R8.6)
- Failure/timeout → failed with reason and no partial results (R8.8)
- cancel_requested at stage boundary → cancelled with no results (R8.9)
"""

from __future__ import annotations

from typing import Any


from rag_system.config import ModelPricing
from rag_system.models import (
    AIConfigurationVersion,
    CorpusSnapshot,
    ReplayRetrievalParams,
    ReplayRun,
    ReplayRunRequest,
    ReplayRunState,
    SqlResultFixture,
)
from rag_system.replay import (
    ReplayWorker,
    RetrievalResult,
    normalized_sql_hash,
)
from rag_system.storage import (
    PreconditionFailed,
    ai_config_version_key,
    corpus_snapshot_key,
    replay_run_key,
    sql_result_fixture_key,
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

    from rag_system.storage import GcsArtifactStore

    create_json = GcsArtifactStore.create_json
    update_json_cas = GcsArtifactStore.update_json_cas


# ---------------------------------------------------------------------------
# Fake executor
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """Fake replay executor that returns controlled results."""

    def __init__(
        self,
        route: str = "rag",
        retrieval_hits: list[dict[str, Any]] | None = None,
        retrieval_scores: list[float] | None = None,
        answer: str = "The answer is 42.",
        evidence: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        sql_query: str = "SELECT * FROM docs",
        *,
        raise_on_classify: Exception | None = None,
        raise_on_retrieve: Exception | None = None,
        raise_on_generate: Exception | None = None,
        raise_on_generate_sql: Exception | None = None,
    ) -> None:
        self.route = route
        self.retrieval_hits = retrieval_hits or []
        self.retrieval_scores = retrieval_scores or [0.85, 0.72]
        self.answer = answer
        self.evidence = evidence or []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.sql_query = sql_query
        self.raise_on_classify = raise_on_classify
        self.raise_on_retrieve = raise_on_retrieve
        self.raise_on_generate = raise_on_generate
        self.raise_on_generate_sql = raise_on_generate_sql

        # Track calls for assertion
        self.classify_calls: list[tuple[str, str]] = []
        self.retrieve_calls: list[dict[str, Any]] = []
        self.generate_calls: list[dict[str, Any]] = []
        self.generate_sql_calls: list[tuple[str, str]] = []

    def classify_route(
        self,
        question: str,
        config: AIConfigurationVersion,
    ) -> str:
        self.classify_calls.append((question, config.version_id))
        if self.raise_on_classify:
            raise self.raise_on_classify
        return self.route

    def retrieve_snapshot_scoped(
        self,
        question: str,
        config: AIConfigurationVersion,
        manifest: list[tuple[str, str]],
        *,
        max_passages: int,
        min_score: float,
    ) -> RetrievalResult:
        self.retrieve_calls.append({
            "question": question,
            "config_version": config.version_id,
            "manifest": manifest,
            "max_passages": max_passages,
            "min_score": min_score,
        })
        if self.raise_on_retrieve:
            raise self.raise_on_retrieve
        return RetrievalResult(
            hits=self.retrieval_hits,
            scores=self.retrieval_scores,
        )

    def generate_answer(
        self,
        question: str,
        config: AIConfigurationVersion,
        route: str,
        retrieval_hits: list[dict[str, Any]],
        sql_rows: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        self.generate_calls.append({
            "question": question,
            "route": route,
            "retrieval_hits": retrieval_hits,
            "sql_rows": sql_rows,
        })
        if self.raise_on_generate:
            raise self.raise_on_generate
        return {
            "answer": self.answer,
            "evidence": self.evidence,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def generate_sql(
        self,
        question: str,
        config: AIConfigurationVersion,
    ) -> str:
        self.generate_sql_calls.append((question, config.version_id))
        if self.raise_on_generate_sql:
            raise self.raise_on_generate_sql
        return self.sql_query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRICING_MAP: dict[str, ModelPricing] = {
    "gemini-3.5-flash": ModelPricing(
        prompt_usd_per_1k=0.000075, completion_usd_per_1k=0.0003
    ),
    "gemini-3.1-pro": ModelPricing(
        prompt_usd_per_1k=0.00125, completion_usd_per_1k=0.005
    ),
}


def _seed_approved_version(
    store: _FakeStore,
    config_id: str = "default",
    version_id: str = "v1",
    model: str = "gemini-3.5-flash",
) -> AIConfigurationVersion:
    version = AIConfigurationVersion(
        config_id=config_id,
        version_id=version_id,
        prompt="answer the question",
        model=model,
        output_schema={},
        router_threshold=0.5,
        retrieval_settings={},
        change_description="initial approved config",
        created_at="2024-01-01T00:00:00+00:00",
        approved=True,
        approver="operator@example.com",
        approved_at="2024-01-01T01:00:00+00:00",
    )
    key = ai_config_version_key(config_id, version_id)
    store.objects[key] = (version.model_dump(), '"etag-seed"')
    return version


def _seed_snapshot(
    store: _FakeStore,
    snapshot_id: str = "snap-1",
    manifest: list[tuple[str, str]] | None = None,
) -> CorpusSnapshot:
    snapshot = CorpusSnapshot(
        corpus_snapshot_id=snapshot_id,
        created_at="2024-01-01T00:00:00+00:00",
        manifest=manifest or [("doc-a", "v1"), ("doc-b", "v2")],
    )
    key = corpus_snapshot_key(snapshot_id)
    store.objects[key] = (snapshot.model_dump(), '"etag-seed"')
    return snapshot


def _seed_sql_fixture(
    store: _FakeStore,
    corpus_snapshot_id: str = "snap-1",
    sql: str = "SELECT * FROM docs",
    rows: list[dict[str, Any]] | None = None,
) -> SqlResultFixture:
    sql_hash = normalized_sql_hash(sql)
    fixture = SqlResultFixture(
        fixture_id=sql_hash,
        corpus_snapshot_id=corpus_snapshot_id,
        sql=sql,
        normalized_sql_hash=sql_hash,
        rows=rows or [{"id": 1, "name": "test"}],
    )
    key = sql_result_fixture_key(corpus_snapshot_id, sql_hash)
    store.objects[key] = (fixture.model_dump(), '"etag-seed"')
    return fixture


def _seed_queued_run(
    store: _FakeStore,
    run_id: str = "run-1",
    question: str = "What is the answer?",
    version_id: str = "default:v1",
    snapshot_id: str = "snap-1",
    cancel_requested: bool = False,
) -> ReplayRun:
    run = ReplayRun(
        replay_run_id=run_id,
        state=ReplayRunState.queued,
        request=ReplayRunRequest(
            question=question,
            ai_configuration_version_id=version_id,
            retrieval_params=ReplayRetrievalParams(
                max_passages=10,
                min_score=0.5,
            ),
            corpus_snapshot_id=snapshot_id,
        ),
        cancel_requested=cancel_requested,
    )
    key = replay_run_key(run_id)
    store.objects[key] = (run.model_dump(), '"etag-seed"')
    return run


def _make_worker(
    store: _FakeStore,
    executor: _FakeExecutor | None = None,
    timeout_s: int = 300,
) -> ReplayWorker:
    return ReplayWorker(
        store=store,
        executor=executor or _FakeExecutor(),
        timeout_s=timeout_s,
        pricing_map=_PRICING_MAP,
    )


# ---------------------------------------------------------------------------
# Tests — successful execution (R8.5, R8.7)
# ---------------------------------------------------------------------------


def test_execute_run_transitions_queued_to_completed() -> None:
    """A queued run transitions to completed with full results recorded."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    executor = _FakeExecutor(
        retrieval_scores=[0.85, 0.72],
        prompt_tokens=100,
        completion_tokens=50,
    )
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.completed
    assert result_run.result is not None
    assert result_run.result.answer == "The answer is 42."
    assert result_run.result.route == "rag"
    assert result_run.result.retrieval_scores == [0.85, 0.72]
    assert result_run.result.prompt_tokens == 100
    assert result_run.result.completion_tokens == 50
    assert result_run.result.latency_ms >= 0.0
    assert result_run.result.cost >= 0.0
    assert result_run.failure_reason is None


def test_execute_run_records_correct_cost() -> None:
    """Cost is computed from prompt/completion tokens and model pricing."""
    store = _FakeStore()
    _seed_approved_version(store, model="gemini-3.5-flash")
    _seed_snapshot(store)
    _seed_queued_run(store)
    executor = _FakeExecutor(prompt_tokens=1000, completion_tokens=500)
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    # 1000/1000 * 0.000075 + 500/1000 * 0.0003 = 0.000075 + 0.00015 = 0.000225
    assert result_run.result is not None
    assert abs(result_run.result.cost - 0.000225) < 1e-9


def test_execute_run_persists_completed_state() -> None:
    """The completed run is persisted to the store."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    worker = _make_worker(store)

    worker.execute_run("run-1")

    persisted = store.get_json(replay_run_key("run-1"))
    assert persisted is not None
    restored = ReplayRun.model_validate(persisted)
    assert restored.state == ReplayRunState.completed
    assert restored.result is not None


def test_execute_run_passes_manifest_to_retrieval() -> None:
    """Snapshot-scoped retrieval uses the manifest document/version pairs."""
    store = _FakeStore()
    _seed_approved_version(store)
    manifest = [("doc-x", "v3"), ("doc-y", "v4")]
    _seed_snapshot(store, manifest=manifest)
    _seed_queued_run(store)
    executor = _FakeExecutor()
    worker = _make_worker(store, executor)

    worker.execute_run("run-1")

    assert len(executor.retrieve_calls) == 1
    call = executor.retrieve_calls[0]
    assert call["manifest"] == manifest
    assert call["max_passages"] == 10
    assert call["min_score"] == 0.5


def test_execute_run_with_evidence_items() -> None:
    """Evidence items from generation are included in the result."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    evidence_data = [
        {
            "kind": "document",
            "verification_result": "entails",
            "coverage": "full",
            "quote": "Test quote",
            "source_start": 0,
            "source_end": 10,
            "document_id": "doc-a",
            "document_version": "v1",
        }
    ]
    executor = _FakeExecutor(evidence=evidence_data)
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.result is not None
    assert len(result_run.result.evidence) == 1
    assert result_run.result.evidence[0].kind == "document"
    assert result_run.result.evidence[0].quote == "Test quote"


def test_execute_run_clamps_retrieval_scores() -> None:
    """Retrieval scores are clamped to [0.00, 1.00]."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    executor = _FakeExecutor(retrieval_scores=[-0.1, 0.5, 1.2])
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.result is not None
    assert result_run.result.retrieval_scores == [0.0, 0.5, 1.0]


# ---------------------------------------------------------------------------
# Tests — SQL route and fixture lookup (R8.6)
# ---------------------------------------------------------------------------


def test_sql_route_uses_fixture_rows() -> None:
    """SQL-route replay uses fixture rows, never live data."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    fixture_rows = [{"id": 1, "value": "hello"}, {"id": 2, "value": "world"}]
    _seed_sql_fixture(store, rows=fixture_rows)
    executor = _FakeExecutor(route="database")
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.completed
    # Verify the fixture rows were passed to generate_answer
    assert len(executor.generate_calls) == 1
    assert executor.generate_calls[0]["sql_rows"] == fixture_rows


def test_sql_route_missing_fixture_fails_run() -> None:
    """Missing SQL fixture fails the run with a descriptive failure_reason."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    # No fixture seeded
    executor = _FakeExecutor(route="database")
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.failed
    assert result_run.result is None
    assert result_run.failure_reason is not None
    assert "fixture" in result_run.failure_reason.lower()
    assert "not found" in result_run.failure_reason.lower()


def test_sql_route_fixture_lookup_uses_normalized_hash() -> None:
    """Fixture lookup uses normalized SQL hash, not raw SQL."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    # Seed fixture with normalized SQL
    _seed_sql_fixture(store, sql="SELECT * FROM docs")
    # Executor returns same SQL with different casing/whitespace
    executor = _FakeExecutor(
        route="database", sql_query="  select  *  from  docs  "
    )
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    # Should match because normalized hash is the same
    assert result_run.state == ReplayRunState.completed


# ---------------------------------------------------------------------------
# Tests — failure/timeout (R8.8)
# ---------------------------------------------------------------------------


def test_execution_error_fails_run_with_reason() -> None:
    """An exception during execution fails the run with no partial results."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    executor = _FakeExecutor(raise_on_generate=RuntimeError("LLM unavailable"))
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.failed
    assert result_run.result is None
    assert result_run.failure_reason is not None
    assert "LLM unavailable" in result_run.failure_reason


def test_timeout_fails_run_with_reason() -> None:
    """A timed-out run is marked failed with a timeout message."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    # Use a 0-second timeout so it expires immediately
    executor = _FakeExecutor()
    worker = _make_worker(store, executor, timeout_s=0)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.failed
    assert result_run.result is None
    assert result_run.failure_reason is not None
    assert "timed out" in result_run.failure_reason.lower()


def test_failed_run_is_persisted() -> None:
    """A failed run is persisted to the store."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)
    executor = _FakeExecutor(raise_on_classify=RuntimeError("route error"))
    worker = _make_worker(store, executor)

    worker.execute_run("run-1")

    persisted = store.get_json(replay_run_key("run-1"))
    assert persisted is not None
    restored = ReplayRun.model_validate(persisted)
    assert restored.state == ReplayRunState.failed
    assert restored.result is None
    assert "route error" in restored.failure_reason


# ---------------------------------------------------------------------------
# Tests — cancellation (R8.9)
# ---------------------------------------------------------------------------


def test_cancel_requested_before_execution() -> None:
    """A run with cancel_requested before execution transitions to cancelled."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store, cancel_requested=True)
    executor = _FakeExecutor()
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.cancelled
    assert result_run.result is None
    assert result_run.failure_reason is None
    # Executor was never called
    assert len(executor.classify_calls) == 0


def test_cancel_requested_during_execution() -> None:
    """A cancel_requested set mid-execution stops at stage boundary."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)

    # Set cancel_requested after classify is called (simulate external cancel)
    class _CancellingExecutor(_FakeExecutor):
        def classify_route(self, question, config):
            # After classify, set cancel_requested in the store
            run_data = store.get_json(replay_run_key("run-1"))
            run_data["cancel_requested"] = True
            store.objects[replay_run_key("run-1")] = (
                run_data,
                store._next_etag(),
            )
            return super().classify_route(question, config)

    executor = _CancellingExecutor()
    worker = _make_worker(store, executor)

    result_run = worker.execute_run("run-1")

    assert result_run.state == ReplayRunState.cancelled
    assert result_run.result is None
    assert result_run.failure_reason is None


def test_cancelled_run_is_persisted() -> None:
    """A cancelled run is persisted to the store with no results."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store, cancel_requested=True)
    worker = _make_worker(store)

    worker.execute_run("run-1")

    persisted = store.get_json(replay_run_key("run-1"))
    assert persisted is not None
    restored = ReplayRun.model_validate(persisted)
    assert restored.state == ReplayRunState.cancelled
    assert restored.result is None


# ---------------------------------------------------------------------------
# Tests — non-queued/missing run handling
# ---------------------------------------------------------------------------


def test_execute_run_skips_non_queued_run() -> None:
    """A run already in running/completed/failed state is skipped."""
    store = _FakeStore()
    run = ReplayRun(
        replay_run_id="run-2",
        state=ReplayRunState.running,
        request=ReplayRunRequest(
            question="q",
            ai_configuration_version_id="default:v1",
            retrieval_params=ReplayRetrievalParams(max_passages=10, min_score=0.5),
            corpus_snapshot_id="snap-1",
        ),
    )
    store.objects[replay_run_key("run-2")] = (run.model_dump(), '"etag"')
    worker = _make_worker(store)

    result_run = worker.execute_run("run-2")

    assert result_run.state == ReplayRunState.running  # unchanged


def test_execute_run_handles_missing_run() -> None:
    """A non-existent run returns a failed placeholder."""
    store = _FakeStore()
    worker = _make_worker(store)

    result_run = worker.execute_run("nonexistent")

    assert result_run.state == ReplayRunState.failed
    assert "not found" in (result_run.failure_reason or "").lower()


# ---------------------------------------------------------------------------
# Tests — running state is persisted mid-execution
# ---------------------------------------------------------------------------


def test_running_state_is_persisted_before_execution() -> None:
    """The run is persisted in running state before execution begins."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store)

    states_seen: list[str] = []

    class _StateTrackingExecutor(_FakeExecutor):
        def classify_route(self, question, config):
            # Check the persisted state mid-execution
            persisted = store.get_json(replay_run_key("run-1"))
            restored = ReplayRun.model_validate(persisted)
            states_seen.append(restored.state)
            return super().classify_route(question, config)

    executor = _StateTrackingExecutor()
    worker = _make_worker(store, executor)

    worker.execute_run("run-1")

    # During execution the persisted state should have been 'running'
    assert ReplayRunState.running in states_seen


# ---------------------------------------------------------------------------
# Tests — cancel/complete race (CAS-safe transitions)
# ---------------------------------------------------------------------------


def test_concurrent_cancel_during_finalization_is_not_clobbered() -> None:
    """A cancel that lands during the final stage wins over completion (no result).

    Regression: transitions previously overwrote the stored state blindly, so a
    completion could clobber a concurrent cancellation (or vice versa). The
    finalize transition now reads the current state under CAS and honors a
    ``cancel_requested`` flag set mid-execution.
    """
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store, run_id="run-race")

    class _CancelDuringGenerate(_FakeExecutor):
        def generate_answer(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            # Simulate a cancel request arriving concurrently, after the last
            # stage-boundary check but before the completion is persisted.
            key = replay_run_key("run-race")
            payload, etag = store.objects[key]
            run = ReplayRun.model_validate(payload)
            run.cancel_requested = True
            store.objects[key] = (run.model_dump(), etag)
            return super().generate_answer(*args, **kwargs)

    worker = _make_worker(store, _CancelDuringGenerate())
    result = worker.execute_run("run-race")

    assert result.state == ReplayRunState.cancelled
    assert result.result is None
    # The persisted record must also be cancelled, not completed.
    persisted = ReplayRun.model_validate(store.objects[replay_run_key("run-race")][0])
    assert persisted.state == ReplayRunState.cancelled
    assert persisted.result is None


def test_completed_run_is_immutable_under_transition() -> None:
    """Re-running a completed run never rewrites its recorded result."""
    store = _FakeStore()
    _seed_approved_version(store)
    _seed_snapshot(store)
    _seed_queued_run(store, run_id="run-done")

    worker = _make_worker(store, _FakeExecutor(answer="original answer"))
    first = worker.execute_run("run-done")
    assert first.state == ReplayRunState.completed

    # A second execute_run call finds a non-queued run and skips it; the stored
    # result is preserved verbatim.
    second = worker.execute_run("run-done")
    assert second.state == ReplayRunState.completed
    assert second.result is not None
    assert second.result.answer == "original answer"
