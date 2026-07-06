"""Unit tests for AIConfigResolver (R9.1, R9.2).

Covers:
- Resolution of an existing active version returns its settings bundle.
- Bootstrap: when no active version exists, a seeded default is created and
  activated from config.py defaults.
- Unresolved: dangling active pointer returns an unresolved result (R9.2).
- Unresolved: store exception returns an unresolved result (R9.2).
- The resolved version_id is what the tracing service would stamp.
- Bootstrap is idempotent (second resolve reuses the already-active version).
"""

from __future__ import annotations



from rag_system.ai_config import (
    AIConfigResolver,
    AIConfigurationStore,
    DEFAULT_CONFIG_ID,
    UNRESOLVED_VERSION_ID,
    ResolvedConfig,
    _DEFAULT_PROMPT,
)
from rag_system.models import AIConfigurationVersion
from rag_system.storage import PreconditionFailed


class _FakeStore:
    """In-memory stand-in for the JsonStore protocol."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str]] = {}
        self._counter = 0
        self._bucket = "test-bucket"

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


def _make_store_and_resolver() -> tuple[AIConfigurationStore, AIConfigResolver]:
    fake = _FakeStore()
    config_store = AIConfigurationStore(fake)
    resolver = AIConfigResolver(config_store)
    return config_store, resolver


# ---------------------------------------------------------------------------
# Resolution of an existing active version (R9.1)
# ---------------------------------------------------------------------------


def test_resolve_returns_active_version_settings() -> None:
    """When an active version exists, resolve returns its full settings bundle."""
    config_store, resolver = _make_store_and_resolver()

    # Create and activate a version.
    config_store.create_version(
        "cfg",
        prompt="custom prompt",
        model="gemini-3.1-pro",
        router_threshold=0.7,
        change_description="custom config",
        retrieval_settings={"retrieval_dense_top_k": 40},
        output_schema={"type": "object"},
        version_id="v-active",
    )
    config_store.rollback(
        "cfg", version_id="v-active", operator="op", reason="activate"
    )

    resolved = resolver.resolve("cfg")

    assert resolved.is_resolved is True
    assert resolved.version_id == "v-active"
    assert resolved.config_id == "cfg"
    assert resolved.prompt == "custom prompt"
    assert resolved.model == "gemini-3.1-pro"
    assert resolved.router_threshold == 0.7
    assert resolved.retrieval_settings == {"retrieval_dense_top_k": 40}
    assert resolved.output_schema == {"type": "object"}


def test_resolve_version_id_is_what_tracing_stamps() -> None:
    """The resolved version_id is exactly what the tracing service stamps (R9.1)."""
    config_store, resolver = _make_store_and_resolver()
    config_store.create_version(
        "cfg",
        prompt="p",
        model="m",
        router_threshold=0.5,
        change_description="first",
        version_id="trace-stamp-id",
    )
    config_store.rollback(
        "cfg", version_id="trace-stamp-id", operator="op", reason="go"
    )

    resolved = resolver.resolve("cfg")
    assert resolved.version_id == "trace-stamp-id"


# ---------------------------------------------------------------------------
# Bootstrap from config.py defaults
# ---------------------------------------------------------------------------


def test_resolve_bootstraps_default_when_no_active_version() -> None:
    """When no active version exists, the resolver creates and activates a default."""
    config_store, resolver = _make_store_and_resolver()

    resolved = resolver.resolve("cfg")

    # Should be resolved successfully.
    assert resolved.is_resolved is True
    assert resolved.version_id != UNRESOLVED_VERSION_ID
    assert resolved.config_id == "cfg"
    assert resolved.prompt == _DEFAULT_PROMPT
    # Model comes from config.py defaults.
    assert resolved.model == "gemini-3.5-flash"
    # Router threshold comes from config.py default route_min_confidence.
    assert resolved.router_threshold == 0.5
    # Retrieval settings populated from config defaults.
    assert "retrieval_dense_top_k" in resolved.retrieval_settings
    assert "retrieval_score_threshold" in resolved.retrieval_settings

    # The version should now be persisted and active.
    index = config_store.get_index("cfg")
    assert index.active_version_id == resolved.version_id
    assert resolved.version_id in index.versions


def test_bootstrap_is_idempotent_second_resolve_reuses_active() -> None:
    """Subsequent resolves use the already-active bootstrapped version."""
    config_store, resolver = _make_store_and_resolver()

    first = resolver.resolve("cfg")
    second = resolver.resolve("cfg")

    assert first.version_id == second.version_id
    # Only one version should exist in history.
    assert len(config_store.get_index("cfg").versions) == 1


def test_bootstrap_uses_default_config_id() -> None:
    """The default config_id is 'default' when not specified."""
    _, resolver = _make_store_and_resolver()
    resolved = resolver.resolve()
    assert resolved.config_id == DEFAULT_CONFIG_ID
    assert resolved.is_resolved is True


# ---------------------------------------------------------------------------
# Unresolved cases (R9.2)
# ---------------------------------------------------------------------------


def test_resolve_unresolved_when_active_version_dangling() -> None:
    """A dangling active_version_id (version object missing) returns unresolved."""
    fake = _FakeStore()
    config_store = AIConfigurationStore(fake)
    resolver = AIConfigResolver(config_store)

    # Manually write an index with a dangling active pointer.
    from rag_system.storage import ai_config_index_key

    fake.objects[ai_config_index_key("cfg")] = (
        {
            "config_id": "cfg",
            "active_version_id": "missing-version",
            "versions": ["missing-version"],
            "activation_events": [],
        },
        '"etag-manual"',
    )

    resolved = resolver.resolve("cfg")

    assert resolved.is_resolved is False
    assert resolved.version_id == UNRESOLVED_VERSION_ID
    assert resolved.config_id == "cfg"
    # Safe defaults are populated so the pipeline can still function.
    assert resolved.prompt == _DEFAULT_PROMPT
    assert resolved.model == "gemini-3.5-flash"
    assert resolved.router_threshold == 0.5


def test_resolve_unresolved_on_store_exception() -> None:
    """Any unexpected store error results in an unresolved config (R9.2)."""

    class _ExplodingStore:
        def get_json(self, key: str) -> object | None:
            raise RuntimeError("simulated storage failure")

        def get_json_with_etag(self, key: str) -> tuple[object | None, str | None]:
            raise RuntimeError("simulated storage failure")

        def create_json(self, key: str, payload: object) -> str:
            raise RuntimeError("simulated storage failure")

        def update_json_cas(self, key, mutate, *, max_attempts=5):
            raise RuntimeError("simulated storage failure")

    config_store = AIConfigurationStore(_ExplodingStore())
    resolver = AIConfigResolver(config_store)

    resolved = resolver.resolve("cfg")

    assert resolved.is_resolved is False
    assert resolved.version_id == UNRESOLVED_VERSION_ID


# ---------------------------------------------------------------------------
# ResolvedConfig helpers
# ---------------------------------------------------------------------------


def test_resolved_config_from_version() -> None:
    """ResolvedConfig.from_version populates all fields from the version."""
    version = AIConfigurationVersion(
        config_id="c",
        version_id="v",
        prompt="p",
        model="m",
        output_schema={"k": "v"},
        router_threshold=0.8,
        retrieval_settings={"a": 1},
        change_description="test",
        created_at="2024-01-01T00:00:00+00:00",
    )
    rc = ResolvedConfig.from_version(version)
    assert rc.version_id == "v"
    assert rc.config_id == "c"
    assert rc.prompt == "p"
    assert rc.model == "m"
    assert rc.output_schema == {"k": "v"}
    assert rc.router_threshold == 0.8
    assert rc.retrieval_settings == {"a": 1}
    assert rc.is_resolved is True


def test_resolved_config_unresolved() -> None:
    """ResolvedConfig.unresolved returns safe defaults with is_resolved=False."""
    rc = ResolvedConfig.unresolved("my-cfg")
    assert rc.is_resolved is False
    assert rc.version_id == UNRESOLVED_VERSION_ID
    assert rc.config_id == "my-cfg"
    assert rc.prompt == _DEFAULT_PROMPT
    assert rc.model == "gemini-3.5-flash"
    assert rc.router_threshold == 0.5
    assert rc.retrieval_settings == {}
    assert rc.output_schema == {}
