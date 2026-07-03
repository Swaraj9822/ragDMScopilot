"""Property test for AI configuration immutability and ordered history (R9.5, R9.7).

Feature: rag-trust-and-observability, Property 33: AI configuration versions are immutable and history is ordered

Validates:

- *For any* created ``AI_Configuration_Version``, no subsequent operation
  modifies it (R9.7): a second create-only write to the same key is rejected,
  and re-reading the version after arbitrary store operations (create more
  versions, rollbacks) returns bit-for-bit the same data.
- The history endpoint returns all versions with their change descriptions in
  reverse-chronological order (R9.5).
- Round-trip serialization (``model_dump`` → ``model_validate``) preserves all
  fields of an ``AIConfigurationVersion`` exactly.

The service is exercised against an in-memory store double that binds the real
create-only / ETag-CAS primitives, so immutability is verified against genuine
persistence semantics rather than a mock.
"""

from __future__ import annotations

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from rag_system.ai_config import AIConfigurationStore
from rag_system.models import AIConfigurationVersion
from rag_system.storage import PreconditionFailed


# ---------------------------------------------------------------------------
# In-memory store double (same as used in the sibling property-test file)
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in exposing the CAS primitives the service relies on."""

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

    from rag_system.storage import S3ArtifactStore

    create_json = S3ArtifactStore.create_json
    update_json_cas = S3ArtifactStore.update_json_cas


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid change descriptions (1–500 chars).
_descriptions = st.text(min_size=1, max_size=500)

# Lightweight settings bundle — we don't need complex content, just field
# population for the round-trip and immutability checks.
_prompts = st.text(min_size=1, max_size=200)
_models = st.sampled_from(["gemini-3.5-flash", "gemini-3.1-pro", "custom-model-v2"])
_thresholds = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_schemas = st.fixed_dictionaries({}, optional={"key": st.text(max_size=20)})
_settings_dicts = st.fixed_dictionaries(
    {},
    optional={
        "top_k": st.integers(1, 100),
        "enabled": st.booleans(),
    },
)

# Sorted, unique timestamps so we can verify ordering.
_timestamps = st.lists(
    st.from_regex(r"2024-0[1-9]-[012][0-9]T00:00:00\+00:00", fullmatch=True),
    min_size=1,
    max_size=8,
    unique=True,
)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 33: AI configuration versions are immutable and history is ordered
# Validates: Requirements 9.5, 9.7
@settings(max_examples=100)
@given(
    descriptions=st.lists(_descriptions, min_size=1, max_size=6),
    prompts=st.lists(_prompts, min_size=1, max_size=6),
    model=_models,
    threshold=_thresholds,
)
def test_versions_are_immutable_after_creation(
    descriptions: list[str],
    prompts: list[str],
    model: str,
    threshold: float,
) -> None:
    """Once created, an AI configuration version cannot be modified (R9.7).

    For each created version, re-reading it after subsequent operations returns
    the exact same data, and a second create-only write to the same key is
    rejected (PreconditionFailed).
    """
    fake = _FakeStore()
    store = AIConfigurationStore(fake)

    # Create N versions, keeping a snapshot of each as-created.
    created_versions: list[AIConfigurationVersion] = []
    for i, desc in enumerate(descriptions):
        prompt = prompts[i % len(prompts)]
        version = store.create_version(
            "cfg",
            prompt=prompt,
            model=model,
            router_threshold=threshold,
            change_description=desc,
            version_id=f"v{i}",
            created_at=f"2024-0{(i % 9) + 1}-15T00:00:00+00:00",
        )
        created_versions.append(version)

    # Perform some operations on the store (rollbacks, more creates) that must
    # not mutate any existing version.
    if len(created_versions) >= 2:
        store.rollback("cfg", version_id="v0", operator="op", reason="rollback")
        store.rollback("cfg", version_id="v1", operator="op", reason="forward")

    # Verify immutability: re-read each version and compare to the as-created
    # snapshot.
    for original in created_versions:
        reloaded = store.get_version("cfg", original.version_id)
        assert reloaded is not None, f"version {original.version_id} must still exist"
        assert reloaded.model_dump() == original.model_dump(), (
            f"version {original.version_id} was mutated after creation"
        )

    # Verify the create-only write primitive rejects a second write.
    from rag_system.storage import ai_config_version_key

    for original in created_versions:
        key = ai_config_version_key("cfg", original.version_id)
        with_pytest_raises = False
        try:
            fake.create_json(key, {"tampered": True})
        except PreconditionFailed:
            with_pytest_raises = True
        assert with_pytest_raises, (
            f"create-only write should reject a duplicate for {original.version_id}"
        )


# Feature: rag-trust-and-observability, Property 33: AI configuration versions are immutable and history is ordered
# Validates: Requirements 9.5, 9.7
@settings(max_examples=100)
@given(
    timestamps=_timestamps,
    descriptions=st.lists(_descriptions, min_size=1, max_size=8),
)
def test_history_returns_versions_in_reverse_chronological_order(
    timestamps: list[str],
    descriptions: list[str],
) -> None:
    """History returns versions ordered by creation timestamp, most recent first (R9.5).

    For any set of versions created with distinct timestamps (possibly inserted
    in any order), ``get_history`` returns them sorted reverse-chronologically.
    """
    store = AIConfigurationStore(_FakeStore())

    # Align descriptions to timestamps (take the shorter length).
    n = min(len(timestamps), len(descriptions))
    assume(n >= 1)
    timestamps = timestamps[:n]
    descriptions = descriptions[:n]

    # Insert versions in arbitrary order (timestamp order need not match
    # insertion order — the service must sort by created_at regardless).
    created: list[AIConfigurationVersion] = []
    for i, (ts, desc) in enumerate(zip(timestamps, descriptions)):
        version = store.create_version(
            "cfg",
            prompt="p",
            model="gemini-3.5-flash",
            router_threshold=0.5,
            change_description=desc,
            version_id=f"v{i}",
            created_at=ts,
        )
        created.append(version)

    history = store.get_history("cfg")

    # All created versions are present.
    assert len(history) == n

    # Verify reverse-chronological order.
    for i in range(len(history) - 1):
        assert history[i].created_at >= history[i + 1].created_at, (
            f"history[{i}].created_at={history[i].created_at} should be >= "
            f"history[{i + 1}].created_at={history[i + 1].created_at}"
        )

    # Each version's change_description is preserved in the history.
    history_ids = {v.version_id for v in history}
    for v in created:
        assert v.version_id in history_ids
        matched = next(h for h in history if h.version_id == v.version_id)
        assert matched.change_description == v.change_description


# Feature: rag-trust-and-observability, Property 33: AI configuration versions are immutable and history is ordered
# Validates: Requirements 9.5, 9.7
@settings(max_examples=200)
@given(
    prompt=_prompts,
    model=_models,
    threshold=_thresholds,
    description=_descriptions,
    output_schema=_schemas,
    retrieval_settings=_settings_dicts,
    reranker_config=_settings_dicts,
)
def test_round_trip_serialization_preserves_all_fields(
    prompt: str,
    model: str,
    threshold: float,
    description: str,
    output_schema: dict,
    retrieval_settings: dict,
    reranker_config: dict,
) -> None:
    """Serializing then deserializing an AIConfigurationVersion preserves all fields.

    This validates the round-trip contract: ``model_dump`` → ``model_validate``
    yields an identical object, ensuring no field is lost or corrupted during
    persistence.
    """
    version = AIConfigurationVersion(
        config_id="cfg",
        version_id="v-round-trip",
        prompt=prompt,
        model=model,
        output_schema=output_schema,
        router_threshold=threshold,
        retrieval_settings=retrieval_settings,
        reranker_config=reranker_config,
        change_description=description,
        created_at="2024-06-15T12:00:00+00:00",
        approved=False,
        approver=None,
        approved_at=None,
    )

    serialized = version.model_dump()
    deserialized = AIConfigurationVersion.model_validate(serialized)

    assert deserialized == version
    assert deserialized.config_id == version.config_id
    assert deserialized.version_id == version.version_id
    assert deserialized.prompt == version.prompt
    assert deserialized.model == version.model
    assert deserialized.output_schema == version.output_schema
    assert deserialized.router_threshold == version.router_threshold
    assert deserialized.retrieval_settings == version.retrieval_settings
    assert deserialized.reranker_config == version.reranker_config
    assert deserialized.change_description == version.change_description
    assert deserialized.created_at == version.created_at
    assert deserialized.approved == version.approved
    assert deserialized.approver == version.approver
    assert deserialized.approved_at == version.approved_at
