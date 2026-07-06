"""Unit tests for the versioned AI configuration service (R9.3–R9.10).

Covers change-description validation, immutable create-only version writes with
ordered reverse-chronological history, and rollback as an activation-pointer
change plus an audited :class:`ActivationEvent` that retains all prior versions.
"""

from __future__ import annotations

import pytest

from rag_system.ai_config import (
    CHANGE_DESCRIPTION_MAX_LENGTH,
    AIConfigurationStore,
    ChangeDescriptionRequiredError,
    ConfigurationVersionNotFoundError,
    validate_change_description,
)
from rag_system.models import AIConfigurationVersion
from rag_system.storage import (
    PreconditionFailed,
    ai_config_version_key,
)


class _FakeStore:
    """In-memory stand-in exposing the CAS primitives the service relies on.

    Binds the real ``create_json`` / ``update_json_cas`` implementations on top
    of an in-memory ``get_json_with_etag`` / ``put_json_conditional``, mirroring
    the doubles used across the storage/service tests so the create-only and
    ETag-CAS semantics are exercised for real.
    """

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

    # Bind the real, store-agnostic helper implementations.
    from rag_system.storage import GcsArtifactStore

    create_json = GcsArtifactStore.create_json
    update_json_cas = GcsArtifactStore.update_json_cas


def _make_version(
    store: AIConfigurationStore,
    config_id: str = "cfg",
    *,
    change_description: str = "initial config",
    version_id: str | None = None,
    created_at: str | None = None,
    prompt: str = "answer the question",
    model: str = "gemini-3.5-flash",
    router_threshold: float = 0.5,
) -> AIConfigurationVersion:
    return store.create_version(
        config_id,
        prompt=prompt,
        model=model,
        router_threshold=router_threshold,
        change_description=change_description,
        version_id=version_id,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Change-description validation (R9.3, R9.4)
# ---------------------------------------------------------------------------


def test_validate_change_description_accepts_boundary_lengths() -> None:
    assert validate_change_description("x") == "x"
    max_desc = "y" * CHANGE_DESCRIPTION_MAX_LENGTH
    assert validate_change_description(max_desc) == max_desc


@pytest.mark.parametrize(
    "description",
    ["", None, "z" * (CHANGE_DESCRIPTION_MAX_LENGTH + 1)],
)
def test_validate_change_description_rejects_out_of_range(description) -> None:
    with pytest.raises(ChangeDescriptionRequiredError) as excinfo:
        validate_change_description(description)
    assert excinfo.value.code == "change_description_required"


def test_create_version_rejects_invalid_description_without_writing() -> None:
    store = AIConfigurationStore(_FakeStore())
    # Seed one valid version so there is an existing history/active state.
    first = _make_version(store, change_description="valid", version_id="v1")

    with pytest.raises(ChangeDescriptionRequiredError):
        store.create_version(
            "cfg",
            prompt="p",
            model="m",
            router_threshold=0.5,
            change_description="",
            version_id="v2",
        )

    # No new version created, history unchanged (R9.4).
    history = store.get_history("cfg")
    assert [v.version_id for v in history] == [first.version_id]
    assert store.get_version("cfg", "v2") is None


def test_create_version_stores_change_description_and_is_immutable() -> None:
    fake = _FakeStore()
    store = AIConfigurationStore(fake)
    version = _make_version(
        store, change_description="tune router threshold", version_id="v1"
    )
    assert version.change_description == "tune router threshold"

    # The version is persisted create-only; a second write to the same key is
    # rejected, which is exactly the immutability guarantee (R9.7).
    with pytest.raises(PreconditionFailed):
        fake.create_json(ai_config_version_key("cfg", "v1"), {"tampered": True})
    stored, _ = fake.get_json_with_etag(ai_config_version_key("cfg", "v1"))
    assert stored["change_description"] == "tune router threshold"


# ---------------------------------------------------------------------------
# History ordering (R9.5, R9.6)
# ---------------------------------------------------------------------------


def test_history_empty_when_no_versions() -> None:
    store = AIConfigurationStore(_FakeStore())
    assert store.get_history("cfg") == []


def test_history_is_reverse_chronological() -> None:
    store = AIConfigurationStore(_FakeStore())
    _make_version(
        store, version_id="v1", created_at="2024-01-01T00:00:00+00:00",
        change_description="first",
    )
    _make_version(
        store, version_id="v2", created_at="2024-02-01T00:00:00+00:00",
        change_description="second",
    )
    _make_version(
        store, version_id="v3", created_at="2024-03-01T00:00:00+00:00",
        change_description="third",
    )

    history = store.get_history("cfg")
    assert [v.version_id for v in history] == ["v3", "v2", "v1"]
    assert [v.change_description for v in history] == ["third", "second", "first"]


def test_create_version_does_not_change_active_pointer() -> None:
    store = AIConfigurationStore(_FakeStore())
    _make_version(store, version_id="v1")
    # Creating a version records history but does not activate it (R9.3 does not
    # activate; activation is the rollback operation's job).
    assert store.get_index("cfg").active_version_id is None
    assert store.get_index("cfg").versions == ["v1"]


# ---------------------------------------------------------------------------
# Rollback activation + audit + retention (R9.8, R9.9, R9.10)
# ---------------------------------------------------------------------------


def test_rollback_activates_target_and_records_activation_event() -> None:
    store = AIConfigurationStore(_FakeStore())
    _make_version(store, version_id="v1")
    _make_version(store, version_id="v2")

    event = store.rollback(
        "cfg", version_id="v2", operator="op-1", reason="better recall"
    )
    assert event.selected_version_id == "v2"
    assert event.previous_version_id is None
    assert event.operator == "op-1"
    assert event.reason == "better recall"

    index = store.get_index("cfg")
    assert index.active_version_id == "v2"
    assert len(index.activation_events) == 1

    # A second rollback records the previous active as the prior version.
    event2 = store.rollback(
        "cfg", version_id="v1", operator="op-2", reason="revert"
    )
    assert event2.previous_version_id == "v2"
    assert event2.selected_version_id == "v1"
    index = store.get_index("cfg")
    assert index.active_version_id == "v1"
    assert len(index.activation_events) == 2


def test_rollback_unknown_version_leaves_active_unchanged() -> None:
    store = AIConfigurationStore(_FakeStore())
    _make_version(store, version_id="v1")
    store.rollback("cfg", version_id="v1", operator="op", reason="activate")

    with pytest.raises(ConfigurationVersionNotFoundError) as excinfo:
        store.rollback("cfg", version_id="missing", operator="op", reason="x")
    assert excinfo.value.code == "configuration_version_not_found"

    index = store.get_index("cfg")
    assert index.active_version_id == "v1"
    # No spurious activation event recorded for the failed rollback (R9.9).
    assert len(index.activation_events) == 1


def test_rollback_retains_all_prior_versions() -> None:
    store = AIConfigurationStore(_FakeStore())
    for i in range(1, 4):
        _make_version(store, version_id=f"v{i}")

    store.rollback("cfg", version_id="v3", operator="op", reason="latest")
    store.rollback("cfg", version_id="v1", operator="op", reason="rollback")

    index = store.get_index("cfg")
    # Every version id is retained in the ordered history (R9.10) and each
    # underlying version object is still readable and unchanged.
    assert index.versions == ["v1", "v2", "v3"]
    for vid in ("v1", "v2", "v3"):
        assert store.get_version("cfg", vid) is not None


# ---------------------------------------------------------------------------
# Version approval (R8.3, R9.7 — task 15.10)
# ---------------------------------------------------------------------------


def test_approve_version_sets_approval_fields() -> None:
    store = AIConfigurationStore(_FakeStore())
    version = _make_version(store, version_id="v1", prompt="test prompt", model="m1")
    assert version.approved is False
    assert version.approver is None
    assert version.approved_at is None

    approved = store.approve_version("cfg", version_id="v1", approver="op@example.com")
    assert approved.approved is True
    assert approved.approver == "op@example.com"
    assert approved.approved_at is not None


def test_approve_version_does_not_mutate_governed_settings() -> None:
    """Approval must NOT mutate prompt, model, output_schema, router_threshold,
    or retrieval_settings (task 15.10 spec)."""
    store = AIConfigurationStore(_FakeStore())
    original = _make_version(
        store,
        version_id="v1",
        prompt="original prompt",
        model="gemini-3.5-flash",
        router_threshold=0.7,
    )

    approved = store.approve_version("cfg", version_id="v1", approver="admin@co.com")

    assert approved.prompt == original.prompt
    assert approved.model == original.model
    assert approved.router_threshold == original.router_threshold
    assert approved.output_schema == original.output_schema
    assert approved.retrieval_settings == original.retrieval_settings
    assert approved.change_description == original.change_description
    assert approved.version_id == original.version_id
    assert approved.config_id == original.config_id
    assert approved.created_at == original.created_at


def test_approve_version_unknown_version_raises() -> None:
    store = AIConfigurationStore(_FakeStore())
    _make_version(store, version_id="v1")

    with pytest.raises(ConfigurationVersionNotFoundError) as excinfo:
        store.approve_version("cfg", version_id="nonexistent", approver="op@x.com")
    assert excinfo.value.code == "configuration_version_not_found"


def test_approve_version_persists_across_reads() -> None:
    """After approval, subsequent reads of the version reflect the approved state."""
    store = AIConfigurationStore(_FakeStore())
    _make_version(store, version_id="v1")

    store.approve_version("cfg", version_id="v1", approver="admin@co.com")

    reloaded = store.get_version("cfg", "v1")
    assert reloaded is not None
    assert reloaded.approved is True
    assert reloaded.approver == "admin@co.com"
    assert reloaded.approved_at is not None


def test_approve_version_idempotent() -> None:
    """Approving an already-approved version succeeds (updates approver/timestamp)."""
    store = AIConfigurationStore(_FakeStore())
    _make_version(store, version_id="v1")

    first = store.approve_version("cfg", version_id="v1", approver="op1@x.com")
    second = store.approve_version("cfg", version_id="v1", approver="op2@x.com")

    assert second.approved is True
    assert second.approver == "op2@x.com"
    # Governed settings unchanged.
    assert second.prompt == first.prompt
    assert second.model == first.model
