"""Replay and compare lab — snapshots, SQL fixtures, and request validation (R8).

A ``Replay_Run`` re-executes a previously asked question under a chosen
``AI_Configuration_Version`` against a fixed view of the corpus, so experiments
are reproducible. Reproducibility rests on two **immutable, create-only**
artifacts owned by this module:

- :class:`~rag_system.models.CorpusSnapshot` — the exact
  ``(document_id, document_version)`` manifest a run retrieves against, captured
  once so a later ingestion/restore cannot change what a replay sees (R8.1).
- :class:`~rag_system.models.SqlResultFixture` — historical SQL-route rows a run
  reproduces instead of querying live data. A fixture is keyed by
  ``(corpus_snapshot_id, normalized_sql_hash)`` so a SQL-route replay can look up
  the rows it would have produced by normalizing and hashing the query (R8.6).

Both artifacts are written create-only (``if_none_match``), which gives their
immutability for free: a second write to the same key fails its precondition.

Request validation (14.3) ensures that a replay references an **approved**
AI configuration version (prompt/model drawn from it), retrieval params are in
range (max_passages 1–100, min_score 0.00–1.00), and the corpus snapshot exists.
On success a ``queued`` run is created and its id returned without blocking
(R8.2).

Replay worker execution (14.6) transitions ``queued`` → ``running``, executes
the question under the referenced config with snapshot-scoped retrieval, and
records full results on success or a failure reason on failure/timeout.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from rag_system.config import ModelPricing
from rag_system.models import (
    AIConfigurationVersion,
    CorpusSnapshot,
    EvidenceItem,
    ReplayRun,
    ReplayRunRequest,
    ReplayRunResult,
    ReplayRunState,
    SqlResultFixture,
)
from rag_system.observability import get_logger, metrics
from rag_system.storage import corpus_snapshot_key, replay_run_key, sql_result_fixture_key

logger = get_logger(__name__)

#: Random bytes behind a minted ``corpus_snapshot_id``. 16 bytes (128 bits) via
#: ``secrets.token_urlsafe`` is unguessable and collision-free in practice.
_SNAPSHOT_ID_BYTES = 16

#: Collapses any run of whitespace to a single space during SQL normalization.
_WHITESPACE_RE = re.compile(r"\s+")

#: Replay run states that are final — once reached, a run is never rewritten.
_TERMINAL_REPLAY_STATES = frozenset(
    {ReplayRunState.completed, ReplayRunState.failed, ReplayRunState.cancelled}
)


def normalize_sql(sql: str) -> str:
    """Fold insignificant whitespace, case, and a trailing semicolon (R8.6).

    Two SQL strings that differ only in whitespace, letter case, or a trailing
    semicolon normalize to the same value, so they map to the same
    :class:`SqlResultFixture`. This is deliberately a lightweight textual
    normalization (not full SQL parsing): the replay worker keys fixtures by
    hashing the *same* normalization of the query it would otherwise run.
    """
    collapsed = _WHITESPACE_RE.sub(" ", sql).strip()
    while collapsed.endswith(";"):
        collapsed = collapsed[:-1].rstrip()
    return collapsed.casefold()


def normalized_sql_hash(sql: str) -> str:
    """Stable hex digest of the normalized SQL, used to key fixtures (R8.6)."""
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()


class JsonStore(Protocol):
    """The slice of :class:`~rag_system.storage.S3ArtifactStore` this module needs."""

    def create_json(self, key: str, payload: object) -> str: ...

    def get_json(self, key: str) -> object | None: ...

    def update_json_cas(
        self,
        key: str,
        mutate: Callable[[object | None], object],
        *,
        max_attempts: int = ...,
    ) -> object: ...


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReplaySnapshotStore:
    """Create-only persistence for corpus snapshots and SQL fixtures (R8.1, R8.6).

    Both a :class:`CorpusSnapshot` and a :class:`SqlResultFixture` are written
    with a create-only precondition, so once written they cannot be mutated: a
    second write to the same key raises
    :class:`~rag_system.storage.PreconditionFailed`. Snapshot ids are minted
    fresh per capture, so a snapshot write always creates; a fixture is keyed by
    its ``(corpus_snapshot_id, normalized_sql_hash)`` so re-capturing the same
    normalized SQL under the same snapshot is a (rejected) duplicate — exactly
    the immutability guarantee a reproducible replay requires.
    """

    def __init__(self, store: JsonStore):
        self._store = store

    def create_snapshot(
        self,
        manifest: Iterable[Sequence[str]],
        *,
        corpus_snapshot_id: str | None = None,
        created_at: str | None = None,
    ) -> CorpusSnapshot:
        """Persist an immutable corpus snapshot from a version manifest (R8.1).

        ``manifest`` is an iterable of ``(document_id, document_version)`` pairs
        (typically the corpus's current active versions, captured by the
        ``POST /corpus-snapshots`` endpoint). A fresh unguessable
        ``corpus_snapshot_id`` is minted unless one is supplied.
        """
        snapshot_id = corpus_snapshot_id or secrets.token_urlsafe(_SNAPSHOT_ID_BYTES)
        snapshot = CorpusSnapshot(
            corpus_snapshot_id=snapshot_id,
            created_at=created_at or _utcnow_iso(),
            manifest=[(str(doc_id), str(version)) for doc_id, version in manifest],
        )
        # Create-only: immutable once written (a duplicate id would be rejected).
        self._store.create_json(
            corpus_snapshot_key(snapshot_id), snapshot.model_dump()
        )
        logger.info(
            "Created corpus snapshot %s (%d documents)",
            snapshot_id,
            len(snapshot.manifest),
            extra={
                "corpus_snapshot_id": snapshot_id,
                "manifest_size": len(snapshot.manifest),
            },
        )
        metrics.increment("rag_corpus_snapshots_created_total")
        return snapshot

    def list_snapshots(self, keys: Iterable[str]) -> list[CorpusSnapshot]:
        """Load all CorpusSnapshots for the given storage keys.

        Returns a list of :class:`CorpusSnapshot` instances for every key that
        resolves to a valid snapshot record. Keys that do not resolve (deleted or
        corrupt) are silently skipped.
        """
        snapshots: list[CorpusSnapshot] = []
        for key in keys:
            payload = self._store.get_json(key)
            if payload is not None:
                try:
                    snapshots.append(CorpusSnapshot.model_validate(payload))
                except Exception:
                    logger.warning(
                        "Skipping invalid corpus snapshot at key %s", key
                    )
        return snapshots

    def create_sql_fixture(
        self,
        *,
        corpus_snapshot_id: str,
        sql: str,
        rows: Iterable[dict[str, Any]],
    ) -> SqlResultFixture:
        """Persist an immutable SQL result fixture for a snapshot (R8.6).

        The fixture is keyed by ``(corpus_snapshot_id, normalized_sql_hash)``:
        the normalized-SQL hash doubles as the fixture id so the storage key is
        derivable from the query alone at replay time (no separate index). The
        write is create-only, so the historical rows captured here cannot later
        be mutated.
        """
        sql_hash = normalized_sql_hash(sql)
        fixture = SqlResultFixture(
            fixture_id=sql_hash,
            corpus_snapshot_id=corpus_snapshot_id,
            sql=sql,
            normalized_sql_hash=sql_hash,
            rows=list(rows),
        )
        # Create-only: the fixture is immutable once written.
        self._store.create_json(
            sql_result_fixture_key(corpus_snapshot_id, sql_hash),
            fixture.model_dump(),
        )
        logger.info(
            "Created SQL result fixture %s for snapshot %s (%d rows)",
            sql_hash,
            corpus_snapshot_id,
            len(fixture.rows),
            extra={
                "corpus_snapshot_id": corpus_snapshot_id,
                "normalized_sql_hash": sql_hash,
                "row_count": len(fixture.rows),
            },
        )
        metrics.increment("rag_sql_result_fixtures_created_total")
        return fixture


def compute_replay_cost(
    prompt_tokens: int,
    completion_tokens: int,
    model_id: str,
    pricing_map: dict[str, ModelPricing],
) -> float:
    """Compute the USD cost of a replay run from token counts and the pricing map.

    Uses the formula::

        cost = prompt_tokens / 1000 * price_in + completion_tokens / 1000 * price_out

    where ``price_in`` and ``price_out`` are the per-1K-token USD rates for the
    model recorded on the run's ``AI_Configuration_Version``.

    If the model is absent from the pricing map the function returns ``0.0`` and
    logs a warning — an unpriced model never fails the run (R8.7).
    """
    pricing = pricing_map.get(model_id)
    if pricing is None:
        logger.warning(
            "Model %r not found in pricing map; cost defaults to 0.0",
            model_id,
            extra={"model_id": model_id},
        )
        return 0.0

    cost = (
        prompt_tokens / 1000 * pricing.prompt_usd_per_1k
        + completion_tokens / 1000 * pricing.completion_usd_per_1k
    )
    return cost


# ---------------------------------------------------------------------------
# Replay request validation and queued creation (R8.1–R8.4, task 14.3)
# ---------------------------------------------------------------------------

#: Random bytes behind a minted ``replay_run_id``.
_REPLAY_RUN_ID_BYTES = 16


class ReplayValidationError(Exception):
    """Raised when a replay request is invalid (R8.3, R8.4).

    Carries a machine-readable ``code`` and a human-readable ``detail`` naming
    the invalid setting, so the API layer can translate directly to an HTTP 400.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ReplayService:
    """Validates replay requests and creates queued runs (R8.1–R8.4).

    Validation checks, in order:

    1. The referenced ``ai_configuration_version_id`` exists.
    2. The referenced version's ``approved`` flag is ``True`` (R8.3).
    3. The request's prompt/model are drawn from the approved version (R8.3).
    4. ``retrieval_params.max_passages`` is in [1, 100] (R8.4).
    5. ``retrieval_params.min_score`` is in [0.00, 1.00] (R8.4).
    6. The referenced ``corpus_snapshot_id`` exists (R8.4).

    On success a ``queued`` :class:`ReplayRun` is persisted and returned, with
    its identifier, without blocking on execution (R8.2).
    """

    def __init__(self, store: JsonStore, *, config_store: JsonStore | None = None):
        """Initialize the replay service.

        ``store`` is used for replay run persistence and corpus-snapshot
        look-ups. ``config_store`` (defaulting to ``store``) is used to load
        ``AIConfigurationVersion`` records; the indirection lets callers supply
        a separate store that already contains configuration data.
        """
        self._store = store
        self._config_store = config_store or store

    # -- internal helpers ---------------------------------------------------

    def _load_ai_config_version(
        self, config_version_id: str
    ) -> AIConfigurationVersion:
        """Load and return the AI config version, or raise on missing/unapproved.

        The ``config_version_id`` is either ``{config_id}:{version_id}``
        (colon-separated) or a bare ``version_id`` (defaults to config_id
        ``"default"``).
        """
        from rag_system.storage import ai_config_version_key

        if ":" in config_version_id:
            config_id, version_id = config_version_id.split(":", 1)
        else:
            config_id, version_id = "default", config_version_id

        key = ai_config_version_key(config_id, version_id)
        payload = self._config_store.get_json(key)
        if payload is None:
            raise ReplayValidationError(
                code="approved_configuration_required",
                detail=(
                    f"AI configuration version '{config_version_id}' not found. "
                    "An approved AI configuration version is required."
                ),
            )
        return AIConfigurationVersion.model_validate(payload)

    def _load_corpus_snapshot(self, snapshot_id: str) -> CorpusSnapshot:
        """Load and return the snapshot, or raise if it doesn't exist."""
        payload = self._store.get_json(corpus_snapshot_key(snapshot_id))
        if payload is None:
            raise ReplayValidationError(
                code="corpus_snapshot_id",
                detail=(
                    f"Corpus snapshot '{snapshot_id}' does not exist."
                ),
            )
        return CorpusSnapshot.model_validate(payload)

    # -- public API ---------------------------------------------------------

    def cancel_replay_run(self, replay_run_id: str) -> ReplayRun:
        """Cancel a replay run (R8.9).

        Sets ``cancel_requested = True`` and transitions a ``queued`` or
        ``running`` run to ``cancelled`` with no results. If the run is already
        in a terminal state (``completed``, ``failed``, ``cancelled``) this is a
        no-op and the run is returned unchanged.

        Raises :class:`ReplayValidationError` with code ``"not_found"`` if the
        run does not exist.

        The worker additionally checks the ``cancel_requested`` flag at stage
        boundaries, so a ``running`` run that is cancelled here will abort at
        its next boundary even if it cannot be atomically interrupted.
        """
        payload = self._store.get_json(replay_run_key(replay_run_id))
        if payload is None:
            raise ReplayValidationError(
                code="not_found",
                detail=f"Replay run '{replay_run_id}' not found.",
            )

        run = ReplayRun.model_validate(payload)

        # Terminal states are a no-op (idempotent).
        if run.state in _TERMINAL_REPLAY_STATES:
            return run

        holder: dict[str, ReplayRun] = {}

        def _mutate(current: object | None) -> object:
            # Re-check under compare-and-set: a worker may have reached a
            # terminal state between our read above and this write. If so, keep
            # its recorded outcome rather than clobbering it with a cancel.
            if current is None:
                holder["run"] = run
                return run.model_dump()
            cur = ReplayRun.model_validate(current)
            if cur.state in _TERMINAL_REPLAY_STATES:
                holder["run"] = cur
                return current
            cur.cancel_requested = True
            cur.state = ReplayRunState.cancelled
            cur.result = None
            cur.failure_reason = None
            holder["run"] = cur
            return cur.model_dump()

        self._store.update_json_cas(replay_run_key(replay_run_id), _mutate)
        run = holder["run"]

        logger.info(
            "Cancelled replay run %s (state=%s)",
            replay_run_id,
            run.state,
            extra={"replay_run_id": replay_run_id},
        )
        if run.state == ReplayRunState.cancelled:
            metrics.increment("rag_replay_runs_cancelled_total")
        return run

    def create_replay_run(self, request: ReplayRunRequest) -> ReplayRun:
        """Validate a replay request and create a queued run (R8.1–R8.4).

        Returns the persisted :class:`ReplayRun` in the ``queued`` state. Raises
        :class:`ReplayValidationError` with a descriptive code and detail when
        validation fails.
        """
        # 1 & 2. Load the AI configuration version and verify it is approved.
        version = self._load_ai_config_version(
            request.ai_configuration_version_id
        )
        if not version.approved:
            raise ReplayValidationError(
                code="approved_configuration_required",
                detail=(
                    f"AI configuration version "
                    f"'{request.ai_configuration_version_id}' is not approved. "
                    "An approved AI configuration version is required."
                ),
            )

        # 3. Validate retrieval params — Pydantic enforces the range on the
        # model itself (ge=1, le=100 for max_passages; ge=0.0, le=1.0 for
        # min_score), but we add explicit checks here to produce
        # setting-specific error codes (R8.4) for requests that bypass model
        # validation (e.g. from already-constructed objects in tests).
        params = request.retrieval_params
        if not (1 <= params.max_passages <= 100):
            raise ReplayValidationError(
                code="max_passages",
                detail=(
                    f"max_passages must be between 1 and 100 (got "
                    f"{params.max_passages})."
                ),
            )
        if not (0.0 <= params.min_score <= 1.0):
            raise ReplayValidationError(
                code="min_score",
                detail=(
                    f"min_score must be between 0.00 and 1.00 (got "
                    f"{params.min_score})."
                ),
            )

        # 4. Validate corpus snapshot exists.
        self._load_corpus_snapshot(request.corpus_snapshot_id)

        # All validations passed — create a queued run.
        run_id = secrets.token_urlsafe(_REPLAY_RUN_ID_BYTES)
        run = ReplayRun(
            replay_run_id=run_id,
            state=ReplayRunState.queued,
            request=request,
        )

        # Persist under ETag CAS so we own the state-machine transitions.
        def _init(current: object | None) -> object:
            # First write — no prior state expected.
            return run.model_dump()

        self._store.update_json_cas(replay_run_key(run_id), _init)

        logger.info(
            "Created queued replay run %s (config_version=%s, snapshot=%s)",
            run_id,
            request.ai_configuration_version_id,
            request.corpus_snapshot_id,
            extra={
                "replay_run_id": run_id,
                "ai_configuration_version_id": request.ai_configuration_version_id,
                "corpus_snapshot_id": request.corpus_snapshot_id,
            },
        )
        metrics.increment("rag_replay_runs_created_total")
        return run


# ---------------------------------------------------------------------------
# Replay worker execution and lifecycle (R8.5–R8.9, task 14.6)
# ---------------------------------------------------------------------------


class RetrievalResult:
    """Result of a single retrieval pass (snapshot-scoped)."""

    __slots__ = ("hits", "scores")

    def __init__(self, hits: list[dict[str, Any]], scores: list[float]) -> None:
        self.hits = hits
        self.scores = scores


class ReplayExecutor(Protocol):
    """Interface for executing the replay question under a config.

    The executor is responsible for:
    - Classifying the route (rag, database, hybrid)
    - Performing snapshot-scoped retrieval (only document/version pairs in
      the snapshot manifest)
    - Generating the answer and evidence

    The protocol allows injection of fakes in tests.
    """

    def classify_route(
        self,
        question: str,
        config: AIConfigurationVersion,
    ) -> str:
        """Return the selected route for the question."""
        ...

    def retrieve_snapshot_scoped(
        self,
        question: str,
        config: AIConfigurationVersion,
        manifest: list[tuple[str, str]],
        *,
        max_passages: int,
        min_score: float,
    ) -> RetrievalResult:
        """Retrieve passages only against the snapshot manifest's document/version pairs."""
        ...

    def generate_answer(
        self,
        question: str,
        config: AIConfigurationVersion,
        route: str,
        retrieval_hits: list[dict[str, Any]],
        sql_rows: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Generate an answer and return structured output.

        Returns a dict containing:
        - "answer": str
        - "evidence": list of EvidenceItem-compatible dicts
        - "prompt_tokens": int
        - "completion_tokens": int
        """
        ...

    def generate_sql(
        self,
        question: str,
        config: AIConfigurationVersion,
    ) -> str:
        """Generate the SQL query that would be run for the question."""
        ...


class ReplayWorker:
    """Picks up queued replay runs, executes them, and records results (R8.5–R8.9).

    State transitions:
    - ``queued`` → ``running`` → ``completed`` (success)
    - ``queued`` → ``running`` → ``failed``    (execution error or timeout)
    - ``queued`` → ``cancelled``               (cancel_requested before run starts)
    - ``running`` → ``cancelled``              (cancel_requested at stage boundary)

    A missing SQL fixture fails the run with ``failure_reason`` naming the
    missing fixture key. Timeout is governed by ``replay_job_timeout_s``.
    """

    def __init__(
        self,
        store: JsonStore,
        executor: ReplayExecutor,
        *,
        config_store: JsonStore | None = None,
        timeout_s: int = 300,
        pricing_map: dict[str, ModelPricing] | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._config_store = config_store or store
        self._timeout_s = timeout_s
        self._pricing_map = pricing_map or {}

    # -- internal helpers ---------------------------------------------------

    def _load_run(self, run_id: str) -> ReplayRun | None:
        payload = self._store.get_json(replay_run_key(run_id))
        if payload is None:
            return None
        return ReplayRun.model_validate(payload)

    def _transition(self, run: ReplayRun) -> ReplayRun:
        """Persist a state transition via CAS, honoring a concurrent cancel.

        The mutate closure reads the *current* stored state rather than blindly
        overwriting it, which closes two races against
        :meth:`ReplayService.cancel_replay_run`:

        * A run that has already reached a terminal state is never rewritten
          (its recorded result/failure is preserved).
        * A run whose ``cancel_requested`` flag was set concurrently is written
          as ``cancelled`` with no results instead of the intended state — so a
          completion can never clobber a cancellation, nor vice versa.

        Returns the :class:`ReplayRun` actually persisted (which may differ from
        the requested transition when a cancel won the race).
        """
        target_state = run.state
        target_result = run.result
        target_failure = run.failure_reason
        holder: dict[str, ReplayRun] = {}

        def _mutate(current: object | None) -> object:
            if current is None:
                # First write or missing record: trust the in-memory run as-is.
                holder["run"] = run
                return run.model_dump()
            cur = ReplayRun.model_validate(current)
            # Terminal states are immutable — preserve the recorded outcome.
            if cur.state in _TERMINAL_REPLAY_STATES:
                holder["run"] = cur
                return current
            # A concurrent cancel takes precedence over any non-cancel target.
            if cur.cancel_requested and target_state != ReplayRunState.cancelled:
                cur.state = ReplayRunState.cancelled
                cur.result = None
                cur.failure_reason = None
                holder["run"] = cur
                return cur.model_dump()
            # Apply the requested transition, preserving the stored cancel flag.
            cur.state = target_state
            cur.result = target_result
            cur.failure_reason = target_failure
            if target_state == ReplayRunState.cancelled:
                cur.cancel_requested = True
            holder["run"] = cur
            return cur.model_dump()

        self._store.update_json_cas(replay_run_key(run.replay_run_id), _mutate)
        return holder["run"]

    def _check_cancelled(self, run: ReplayRun) -> bool:
        """Reload the run from the store to check the cancel_requested flag.

        Returns True if cancellation is requested (stage boundary check).
        """
        reloaded = self._load_run(run.replay_run_id)
        if reloaded is not None and reloaded.cancel_requested:
            return True
        return False

    def _load_ai_config_version(
        self, config_version_id: str
    ) -> AIConfigurationVersion:
        """Load the AI configuration version for executing the run."""
        from rag_system.storage import ai_config_version_key

        if ":" in config_version_id:
            config_id, version_id = config_version_id.split(":", 1)
        else:
            config_id, version_id = "default", config_version_id

        key = ai_config_version_key(config_id, version_id)
        payload = self._config_store.get_json(key)
        if payload is None:
            raise RuntimeError(
                f"AI configuration version '{config_version_id}' not found "
                "during replay execution."
            )
        return AIConfigurationVersion.model_validate(payload)

    def _load_snapshot(self, snapshot_id: str) -> CorpusSnapshot:
        """Load the corpus snapshot for the run."""
        payload = self._store.get_json(corpus_snapshot_key(snapshot_id))
        if payload is None:
            raise RuntimeError(
                f"Corpus snapshot '{snapshot_id}' not found during replay execution."
            )
        return CorpusSnapshot.model_validate(payload)

    def _lookup_sql_fixture(
        self, corpus_snapshot_id: str, sql: str
    ) -> SqlResultFixture:
        """Look up the SQL result fixture by (snapshot_id, normalized_sql_hash).

        Raises RuntimeError with a descriptive message if the fixture is missing
        — this fails the run per R8.6.
        """
        sql_hash = normalized_sql_hash(sql)
        key = sql_result_fixture_key(corpus_snapshot_id, sql_hash)
        payload = self._store.get_json(key)
        if payload is None:
            raise MissingFixtureError(
                f"SQL result fixture not found for "
                f"corpus_snapshot_id='{corpus_snapshot_id}', "
                f"normalized_sql_hash='{sql_hash}' "
                f"(query: {sql!r}). "
                f"A stored fixture is required for SQL-route replay."
            )
        return SqlResultFixture.model_validate(payload)

    # -- public API ---------------------------------------------------------

    def execute_run(self, run_id: str) -> ReplayRun:
        """Execute a single replay run by id (R8.5–R8.9).

        This is the main entry point called by the worker loop for each queued
        run. It handles the full lifecycle:
        1. Load the run, verify it's queued
        2. Check for cancel_requested before transitioning
        3. Transition to running
        4. Execute under timeout
        5. Record results or failure
        """
        run = self._load_run(run_id)
        if run is None:
            logger.warning("Replay run %s not found; skipping", run_id)
            return ReplayRun.model_construct(
                replay_run_id=run_id,
                state=ReplayRunState.failed,
                request=None,
                failure_reason="Run not found",
            )

        # Only process queued runs
        if run.state != ReplayRunState.queued:
            logger.info(
                "Replay run %s is in state %s (not queued); skipping",
                run_id,
                run.state,
            )
            return run

        # Stage boundary: check cancel_requested before running
        if run.cancel_requested or self._check_cancelled(run):
            run.state = ReplayRunState.cancelled
            run.result = None
            run.failure_reason = None
            self._transition(run)
            logger.info("Replay run %s cancelled before execution", run_id)
            metrics.increment("rag_replay_runs_cancelled_total")
            return run

        # Transition queued → running (honors a concurrent cancel via CAS).
        run.state = ReplayRunState.running
        persisted = self._transition(run)
        if persisted.state != ReplayRunState.running:
            # A cancel (or terminal state) won the race before we started; do
            # not execute. Report whatever was actually persisted.
            logger.info(
                "Replay run %s not started (persisted state=%s)",
                run_id,
                persisted.state,
            )
            if persisted.state == ReplayRunState.cancelled:
                metrics.increment("rag_replay_runs_cancelled_total")
            return persisted
        logger.info("Replay run %s transitioned to running", run_id)

        # Execute under timeout
        start_time = time.monotonic()
        try:
            result = self._execute_with_timeout(run, start_time)
            run.state = ReplayRunState.completed
            run.result = result
            run.failure_reason = None
            persisted = self._transition(run)
            if persisted.state == ReplayRunState.cancelled:
                # A cancel won the race during the final stage — no results kept.
                logger.info("Replay run %s cancelled during execution", run_id)
                metrics.increment("rag_replay_runs_cancelled_total")
                return persisted
            logger.info(
                "Replay run %s completed successfully (latency=%dms, cost=%.6f)",
                run_id,
                result.latency_ms,
                result.cost,
            )
            metrics.increment("rag_replay_runs_completed_total")
            return persisted
        except _CancelledAtBoundary:
            run.state = ReplayRunState.cancelled
            run.result = None
            run.failure_reason = None
            persisted = self._transition(run)
            logger.info("Replay run %s cancelled during execution", run_id)
            metrics.increment("rag_replay_runs_cancelled_total")
            return persisted
        except _TimeoutExpired:
            run.state = ReplayRunState.failed
            run.result = None
            run.failure_reason = (
                f"Replay job timed out after {self._timeout_s}s"
            )
            persisted = self._transition(run)
            logger.warning("Replay run %s timed out", run_id)
            metrics.increment("rag_replay_runs_failed_total")
            return persisted
        except MissingFixtureError as exc:
            run.state = ReplayRunState.failed
            run.result = None
            run.failure_reason = str(exc)
            persisted = self._transition(run)
            logger.warning("Replay run %s failed: %s", run_id, exc)
            metrics.increment("rag_replay_runs_failed_total")
            return persisted
        except Exception as exc:
            run.state = ReplayRunState.failed
            run.result = None
            run.failure_reason = f"Execution error: {exc}"
            persisted = self._transition(run)
            logger.error(
                "Replay run %s failed with unexpected error",
                run_id,
                exc_info=True,
            )
            metrics.increment("rag_replay_runs_failed_total")
            return persisted

    def _execute_with_timeout(
        self, run: ReplayRun, start_time: float
    ) -> ReplayRunResult:
        """Execute the replay question under the referenced config.

        Raises:
        - _TimeoutExpired if the elapsed time exceeds replay_job_timeout_s
        - _CancelledAtBoundary if cancel_requested is detected at a stage boundary
        - MissingFixtureError if a SQL fixture lookup fails
        """
        request = run.request

        # Load config and snapshot
        config = self._load_ai_config_version(request.ai_configuration_version_id)
        snapshot = self._load_snapshot(request.corpus_snapshot_id)

        # Stage boundary check
        self._check_timeout(start_time)
        if self._check_cancelled(run):
            raise _CancelledAtBoundary()

        # Classify route
        route = self._executor.classify_route(request.question, config)

        # Stage boundary check
        self._check_timeout(start_time)
        if self._check_cancelled(run):
            raise _CancelledAtBoundary()

        # Snapshot-scoped retrieval — only against manifest document/version pairs
        retrieval = self._executor.retrieve_snapshot_scoped(
            request.question,
            config,
            snapshot.manifest,
            max_passages=request.retrieval_params.max_passages,
            min_score=request.retrieval_params.min_score,
        )

        # Stage boundary check
        self._check_timeout(start_time)
        if self._check_cancelled(run):
            raise _CancelledAtBoundary()

        # SQL route: look up fixture (never live data)
        sql_rows: list[dict[str, Any]] | None = None
        if route in ("database", "sql"):
            sql_query = self._executor.generate_sql(request.question, config)
            fixture = self._lookup_sql_fixture(
                request.corpus_snapshot_id, sql_query
            )
            sql_rows = fixture.rows

        # Stage boundary check
        self._check_timeout(start_time)
        if self._check_cancelled(run):
            raise _CancelledAtBoundary()

        # Generate answer
        gen_output = self._executor.generate_answer(
            request.question,
            config,
            route,
            retrieval.hits,
            sql_rows,
        )

        # Compute final metrics
        elapsed_ms = (time.monotonic() - start_time) * 1000
        prompt_tokens = gen_output.get("prompt_tokens", 0)
        completion_tokens = gen_output.get("completion_tokens", 0)
        cost = compute_replay_cost(
            prompt_tokens, completion_tokens, config.model, self._pricing_map
        )

        # Build evidence items from generation output
        raw_evidence = gen_output.get("evidence", [])
        evidence: list[EvidenceItem] = []
        for item_data in raw_evidence:
            if isinstance(item_data, EvidenceItem):
                evidence.append(item_data)
            elif isinstance(item_data, dict):
                evidence.append(EvidenceItem.model_validate(item_data))

        # Clamp retrieval scores to [0.00, 1.00]
        retrieval_scores = [
            max(0.0, min(1.0, s)) for s in retrieval.scores
        ]

        return ReplayRunResult(
            answer=gen_output.get("answer", ""),
            evidence=evidence,
            route=route,
            retrieval_scores=retrieval_scores,
            latency_ms=elapsed_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )

    def _check_timeout(self, start_time: float) -> None:
        """Raise _TimeoutExpired if elapsed time exceeds the configured timeout."""
        elapsed = time.monotonic() - start_time
        if elapsed >= self._timeout_s:
            raise _TimeoutExpired()


class MissingFixtureError(Exception):
    """Raised when a required SQL result fixture is not found (R8.6)."""

    pass


class _CancelledAtBoundary(Exception):
    """Internal: raised when cancel_requested is detected at a stage boundary."""

    pass


class _TimeoutExpired(Exception):
    """Internal: raised when the replay job timeout has been exceeded."""

    pass
