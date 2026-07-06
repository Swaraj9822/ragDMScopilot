"""Versioned AI configuration service (R9).

An ``AI_Configuration`` evolves through a series of **immutable**
:class:`~rag_system.models.AIConfigurationVersion` records — each capturing the
full settings bundle (prompt, model, output schema, router threshold, retrieval
settings) plus a 1–500 character change description. A
per-config :class:`~rag_system.models.AIConfigurationIndex` holds the ordered
history (append-only version ids), the active-version pointer, and the audit log
of :class:`~rag_system.models.ActivationEvent`\\ s.

This module owns the write/read side of that flow (R9.3–R9.10):

- :meth:`AIConfigurationStore.create_version` validates the change description
  and writes a new version **create-only**, so a version can never be mutated
  after creation (R9.3, R9.4, R9.7). It appends the id to the ordered history
  but does **not** move the active pointer — activation is the rollback
  operation's job.
- :meth:`AIConfigurationStore.get_history` returns the versions with their
  change descriptions in reverse-chronological order, empty when none
  (R9.5, R9.6).
- :meth:`AIConfigurationStore.rollback` sets an existing version active and
  records an :class:`ActivationEvent` capturing the operator, previous/selected
  versions, timestamp, and reason — never mutating any version and retaining
  all prior versions (R9.8, R9.9, R9.10). An unknown target leaves the active
  pointer unchanged and raises :class:`ConfigurationVersionNotFoundError`.

The version write uses the create-only primitive (immutability for free); the
index write uses ETag compare-and-set so concurrent activations/creations don't
clobber one another. The HTTP endpoints that surface these operations are wired
separately (task 15.5); this module is transport-agnostic.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from rag_system.models import (
    ActivationEvent,
    AIConfigurationIndex,
    AIConfigurationVersion,
)
from rag_system.observability import get_logger, metrics
from rag_system.storage import ai_config_index_key, ai_config_version_key

logger = get_logger(__name__)

#: Random bytes behind a minted ``version_id``. 16 bytes (128 bits) via
#: ``secrets.token_urlsafe`` is unguessable and collision-free in practice.
_VERSION_ID_BYTES = 16

#: Inclusive bounds for a change description (R9.3, R9.4). These mirror the
#: ``Field(min_length=1, max_length=500)`` constraint on
#: :class:`AIConfigurationVersion.change_description`; validating here first lets
#: us return a stable, machine-readable error code instead of a raw
#: ``ValidationError`` and guarantees no version is written when invalid.
CHANGE_DESCRIPTION_MIN_LENGTH = 1
CHANGE_DESCRIPTION_MAX_LENGTH = 500


class AIConfigError(Exception):
    """Base class for AI-configuration errors carrying a stable ``code``.

    The ``code`` is the machine-readable error string the endpoint (task 15.5)
    maps to a structured HTTP error body.
    """

    code = "ai_config_error"


class ChangeDescriptionRequiredError(AIConfigError):
    """Raised when a change description is missing or out of the 1–500 range (R9.4)."""

    code = "change_description_required"


class ConfigurationVersionNotFoundError(AIConfigError):
    """Raised when a rollback targets a version that does not exist (R9.9)."""

    code = "configuration_version_not_found"


class JsonStore(Protocol):
    """The slice of :class:`~rag_system.storage.GcsArtifactStore` this module needs."""

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


def validate_change_description(change_description: str | None) -> str:
    """Return the change description if it is 1–500 characters, else raise (R9.3, R9.4).

    Whitespace is significant (a single space is a valid 1-character
    description), matching the ``Field(min_length=1, max_length=500)`` constraint
    on the model. ``None`` and the empty string are rejected as *required*.
    """
    if change_description is None or not (
        CHANGE_DESCRIPTION_MIN_LENGTH
        <= len(change_description)
        <= CHANGE_DESCRIPTION_MAX_LENGTH
    ):
        raise ChangeDescriptionRequiredError(
            "A change description of 1 to 500 characters is required."
        )
    return change_description


def _as_index(payload: object | None, config_id: str) -> AIConfigurationIndex:
    """Rebuild an :class:`AIConfigurationIndex` from stored JSON (or a fresh one)."""
    if payload is None:
        return AIConfigurationIndex(config_id=config_id)
    return AIConfigurationIndex.model_validate(payload)


class AIConfigurationStore:
    """Persistence for immutable AI configuration versions and their index (R9).

    Versions are written **create-only** (a duplicate id is rejected), giving
    immutability for free (R9.7). The per-config index — ordered history, active
    pointer, activation events — is written under ETag compare-and-set so
    concurrent writers reload and re-apply rather than clobbering each other.
    """

    def __init__(self, store: JsonStore):
        self._store = store

    # -- reads --------------------------------------------------------------

    def get_index(self, config_id: str) -> AIConfigurationIndex:
        """Load the config's index, or an empty one when none exists yet."""
        payload = self._store.get_json(ai_config_index_key(config_id))
        return _as_index(payload, config_id)

    def get_version(
        self, config_id: str, version_id: str
    ) -> AIConfigurationVersion | None:
        """Load a single immutable version, or ``None`` when it does not exist."""
        payload = self._store.get_json(ai_config_version_key(config_id, version_id))
        if payload is None:
            return None
        return AIConfigurationVersion.model_validate(payload)

    def get_history(self, config_id: str) -> list[AIConfigurationVersion]:
        """Return versions in reverse-chronological order; empty when none (R9.5, R9.6).

        Ordering is primarily by ``created_at`` descending, with the append
        order (newest appended first) as a stable tie-break so versions minted
        within the same timestamp still come back newest-first.
        """
        index = self.get_index(config_id)
        loaded: list[tuple[int, AIConfigurationVersion]] = []
        for position, version_id in enumerate(index.versions):
            version = self.get_version(config_id, version_id)
            if version is None:
                # Index references a version whose object is missing — should not
                # happen (versions are immutable and create-only), but never let
                # a dangling reference break history retrieval.
                logger.warning(
                    "AI config %s history references missing version %s",
                    config_id,
                    version_id,
                    extra={"config_id": config_id, "version_id": version_id},
                )
                continue
            loaded.append((position, version))
        loaded.sort(key=lambda pair: (pair[1].created_at, pair[0]), reverse=True)
        return [version for _, version in loaded]

    # -- writes -------------------------------------------------------------

    def create_version(
        self,
        config_id: str,
        *,
        prompt: str,
        model: str,
        router_threshold: float,
        change_description: str,
        output_schema: dict[str, Any] | None = None,
        retrieval_settings: dict[str, Any] | None = None,
        version_id: str | None = None,
        created_at: str | None = None,
    ) -> AIConfigurationVersion:
        """Create a new immutable version for a valid change description (R9.3, R9.4, R9.7).

        Validates the change description **before** writing anything: an
        empty/too-long description raises :class:`ChangeDescriptionRequiredError`
        and no version is created and the active pointer is left untouched
        (R9.4). On success the version is written create-only (immutable, R9.7)
        and its id appended to the ordered history. The active pointer is *not*
        moved here — activation is :meth:`rollback`'s responsibility.
        """
        validate_change_description(change_description)
        version_id = version_id or secrets.token_urlsafe(_VERSION_ID_BYTES)
        version = AIConfigurationVersion(
            config_id=config_id,
            version_id=version_id,
            prompt=prompt,
            model=model,
            output_schema=output_schema or {},
            router_threshold=router_threshold,
            retrieval_settings=retrieval_settings or {},
            change_description=change_description,
            created_at=created_at or _utcnow_iso(),
        )
        # Create-only: immutable once written (a duplicate id would be rejected).
        self._store.create_json(
            ai_config_version_key(config_id, version_id), version.model_dump()
        )

        def mutate(current: object | None) -> object:
            index = _as_index(current, config_id)
            if version_id not in index.versions:
                index.versions.append(version_id)
            # Active pointer deliberately left unchanged (R9.3 does not activate).
            return index.model_dump()

        self._store.update_json_cas(ai_config_index_key(config_id), mutate)

        logger.info(
            "Created AI configuration version %s for config %s",
            version_id,
            config_id,
            extra={"config_id": config_id, "version_id": version_id},
        )
        metrics.increment("rag_ai_config_versions_created_total")
        return version

    def approve_version(
        self,
        config_id: str,
        *,
        version_id: str,
        approver: str,
    ) -> AIConfigurationVersion:
        """Approve a version: set approved=True, record approver and timestamp (R8.3, R9.7).

        Sets approval metadata on the target version without mutating its
        governed settings (prompt, model, output_schema, router_threshold,
        retrieval_settings). If the version does not exist,
        raises :class:`ConfigurationVersionNotFoundError`.
        """
        # Verify the version exists before attempting the CAS update.
        existing = self.get_version(config_id, version_id)
        if existing is None:
            raise ConfigurationVersionNotFoundError(
                f"AI configuration version {version_id!r} was not found."
            )

        approved_at = _utcnow_iso()

        def mutate(current: object | None) -> object:
            if current is None:
                raise ConfigurationVersionNotFoundError(
                    f"AI configuration version {version_id!r} was not found."
                )
            version = AIConfigurationVersion.model_validate(current)
            version.approved = True
            version.approver = approver
            version.approved_at = approved_at
            return version.model_dump()

        result = self._store.update_json_cas(
            ai_config_version_key(config_id, version_id), mutate
        )
        approved_version = AIConfigurationVersion.model_validate(result)

        logger.info(
            "Approved AI configuration version %s for config %s (approver=%s)",
            version_id,
            config_id,
            approver,
            extra={
                "config_id": config_id,
                "version_id": version_id,
                "approver": approver,
            },
        )
        metrics.increment("rag_ai_config_versions_approved_total")
        return approved_version

    def rollback(
        self,
        config_id: str,
        *,
        version_id: str,
        operator: str,
        reason: str,
    ) -> ActivationEvent:
        """Activate an existing version and audit it as an activation (R9.8–R9.10).

        Sets ``version_id`` as the active configuration and appends an
        :class:`ActivationEvent` (operator, previous version, selected version,
        timestamp, reason) to the index. This is a pointer change plus an audit
        record — it never mutates any :class:`AIConfigurationVersion` and never
        removes a version, so all prior versions are retained (R9.10). Rolling
        back to an unknown version leaves the active pointer unchanged and raises
        :class:`ConfigurationVersionNotFoundError` (R9.9).
        """
        # Pre-check against the current index so we can fail without writing when
        # the target is unknown (active pointer must stay unchanged, R9.9).
        if version_id not in self.get_index(config_id).versions:
            raise ConfigurationVersionNotFoundError(
                f"AI configuration version {version_id!r} was not found."
            )

        event_holder: dict[str, ActivationEvent] = {}

        def mutate(current: object | None) -> object:
            index = _as_index(current, config_id)
            # Re-check under the compare-and-set read: a concurrent writer must
            # not let us activate a version that is not in the retained history.
            if version_id not in index.versions:
                raise ConfigurationVersionNotFoundError(
                    f"AI configuration version {version_id!r} was not found."
                )
            event = ActivationEvent(
                operator=operator,
                previous_version_id=index.active_version_id,
                selected_version_id=version_id,
                timestamp=_utcnow_iso(),
                reason=reason,
            )
            index.active_version_id = version_id
            index.activation_events.append(event)
            event_holder["event"] = event
            return index.model_dump()

        self._store.update_json_cas(ai_config_index_key(config_id), mutate)
        event = event_holder["event"]

        logger.info(
            "Rolled back AI config %s to version %s (operator=%s)",
            config_id,
            version_id,
            operator,
            extra={
                "config_id": config_id,
                "version_id": version_id,
                "previous_version_id": event.previous_version_id,
            },
        )
        metrics.increment("rag_ai_config_rollbacks_total")
        return event


# ---------------------------------------------------------------------------
# AI Configuration Resolver (R9.1, R9.2)
# ---------------------------------------------------------------------------

#: The well-known default config id used when no explicit config is specified.
DEFAULT_CONFIG_ID = "default"

#: Sentinel value stamped on a trace when resolution fails entirely (R9.2).
UNRESOLVED_VERSION_ID = "unresolved"

#: Default generation prompt seeded from the existing system prompt in
#: ``generation.py``. Used to bootstrap the initial AI configuration version
#: so there is always a resolvable version to stamp.
_DEFAULT_PROMPT = (
    "You are answering questions over a private business PDF corpus. "
    "Use only the provided context. If the context is insufficient, "
    "say that the available documents do not contain enough evidence."
)


class ResolvedConfig:
    """The result of resolving an AI configuration for the answer pipeline.

    Contains the full settings bundle that governs the answer path — router
    threshold, retrieval settings, prompt, model, and output
    schema — plus the ``version_id`` that the tracing service stamps on the
    trace (R9.1).

    When resolution fails entirely (no version could be loaded), ``is_resolved``
    is ``False`` and the ``version_id`` is the ``UNRESOLVED_VERSION_ID`` sentinel
    (R9.2).
    """

    __slots__ = (
        "version_id",
        "config_id",
        "prompt",
        "model",
        "output_schema",
        "router_threshold",
        "retrieval_settings",
        "is_resolved",
    )

    def __init__(
        self,
        *,
        version_id: str,
        config_id: str,
        prompt: str,
        model: str,
        output_schema: dict[str, Any],
        router_threshold: float,
        retrieval_settings: dict[str, Any],
        is_resolved: bool = True,
    ) -> None:
        self.version_id = version_id
        self.config_id = config_id
        self.prompt = prompt
        self.model = model
        self.output_schema = output_schema
        self.router_threshold = router_threshold
        self.retrieval_settings = retrieval_settings
        self.is_resolved = is_resolved

    @classmethod
    def from_version(cls, version: AIConfigurationVersion) -> "ResolvedConfig":
        """Build a resolved config from a loaded version."""
        return cls(
            version_id=version.version_id,
            config_id=version.config_id,
            prompt=version.prompt,
            model=version.model,
            output_schema=version.output_schema,
            router_threshold=version.router_threshold,
            retrieval_settings=version.retrieval_settings,
            is_resolved=True,
        )

    @classmethod
    def unresolved(cls, config_id: str) -> "ResolvedConfig":
        """Build an unresolved result when no version can be loaded (R9.2).

        All settings fall back to safe defaults so the answer path can still
        function; the tracing service records the ``UNRESOLVED_VERSION_ID``
        sentinel.
        """
        return cls(
            version_id=UNRESOLVED_VERSION_ID,
            config_id=config_id,
            prompt=_DEFAULT_PROMPT,
            model="gemini-3.5-flash",
            output_schema={},
            router_threshold=0.5,
            retrieval_settings={},
            is_resolved=False,
        )


class AIConfigResolver:
    """Resolves the currently active AI configuration version for the pipeline.

    ``resolve(config_id)`` returns a :class:`ResolvedConfig` whose
    ``version_id`` is exactly what the tracing service stamps on the trace
    (R9.1). When no active version exists (fresh install), the resolver
    bootstraps a seeded default ``AIConfigurationVersion`` from ``config.py``
    defaults and persists it as the initial active version, ensuring there is
    always a resolvable version.

    On any internal failure (store read error, corrupted data, etc.) the
    resolver returns an :meth:`ResolvedConfig.unresolved` result (R9.2) —
    the trace then records the ``unresolved`` indicator and retains all other
    data.
    """

    def __init__(self, store: AIConfigurationStore) -> None:
        self._store = store

    def resolve(self, config_id: str = DEFAULT_CONFIG_ID) -> ResolvedConfig:
        """Load the active version and return its settings bundle.

        Resolution algorithm:
        1. Load the config index for ``config_id``.
        2. If the index has an ``active_version_id``, load that version and
           return it as a :class:`ResolvedConfig`.
        3. If no active version exists (``active_version_id is None``),
           bootstrap a seeded default version from ``config.py`` defaults,
           activate it, persist it, and return it.
        4. If the active version cannot be loaded (dangling pointer),
           return an unresolved result (R9.2).
        5. On any unexpected error, return an unresolved result (R9.2).
        """
        try:
            return self._resolve_inner(config_id)
        except Exception:
            logger.exception(
                "AI config resolution failed for %s; returning unresolved",
                config_id,
                extra={"config_id": config_id},
            )
            return ResolvedConfig.unresolved(config_id)

    def _resolve_inner(self, config_id: str) -> ResolvedConfig:
        """Inner resolution logic, raising on failure so the outer handler
        can catch and produce the unresolved fallback."""
        index = self._store.get_index(config_id)

        if index.active_version_id is not None:
            # Active version exists — load it.
            version = self._store.get_version(config_id, index.active_version_id)
            if version is None:
                # Dangling pointer: active_version_id references a version whose
                # object does not exist. This should not happen under normal
                # operation (versions are immutable and create-only) but we
                # handle it gracefully as an unresolved case (R9.2).
                logger.warning(
                    "AI config %s active_version_id %s not found in store; unresolved",
                    config_id,
                    index.active_version_id,
                    extra={
                        "config_id": config_id,
                        "version_id": index.active_version_id,
                    },
                )
                return ResolvedConfig.unresolved(config_id)
            return ResolvedConfig.from_version(version)

        # No active version — bootstrap from config.py defaults.
        return self._bootstrap_default(config_id)

    def _bootstrap_default(self, config_id: str) -> ResolvedConfig:
        """Create and activate a seeded default version from config.py defaults.

        This ensures a fresh install always has a resolvable version to stamp.
        The default version is built from the existing ``config.py`` settings
        (``gemini_model_id``, ``route_min_confidence``, retrieval
        settings) so the resolver returns values consistent with what the system
        would use anyway.
        """
        from rag_system.config import get_settings

        settings = get_settings()

        # Build a default retrieval_settings dict from the relevant config knobs.
        retrieval_settings: dict[str, Any] = {
            "retrieval_dense_top_k": settings.retrieval_dense_top_k,
            "retrieval_sparse_top_k": settings.retrieval_sparse_top_k,
            "retrieval_score_threshold": settings.retrieval_score_threshold,
            "sparse_enabled": settings.sparse_enabled,
        }

        version = self._store.create_version(
            config_id,
            prompt=_DEFAULT_PROMPT,
            model=settings.gemini_model_id,
            router_threshold=settings.route_min_confidence,
            change_description="Initial default configuration (auto-seeded)",
            retrieval_settings=retrieval_settings,
        )

        # Activate the newly created version so subsequent resolves find it.
        self._store.rollback(
            config_id,
            version_id=version.version_id,
            operator="system",
            reason="Bootstrap initial active configuration",
        )

        logger.info(
            "Bootstrapped default AI config version %s for config %s",
            version.version_id,
            config_id,
            extra={"config_id": config_id, "version_id": version.version_id},
        )
        metrics.increment("rag_ai_config_bootstrap_total")
        return ResolvedConfig.from_version(version)
