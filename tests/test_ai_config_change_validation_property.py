"""Property test for AI configuration change-description validation (R9.3, R9.4).

Feature: rag-trust-and-observability, Property 32: AI configuration change validation

Validates that :func:`validate_change_description` and
:meth:`AIConfigurationStore.create_version` honour the 1–500 character
change-description contract:

- *For any* description of 1–500 characters, ``create_version`` creates a new
  immutable :class:`AIConfigurationVersion` storing exactly that description
  (R9.3).
- *For any* description that is empty or exceeds 500 characters,
  ``create_version`` rejects it with :class:`ChangeDescriptionRequiredError`,
  creates no new version, and leaves the active version pointer unchanged
  (R9.4).

The service is exercised against an in-memory store double that binds the real
create-only / ETag-CAS primitives, so the "no version written" guarantee is
verified against genuine persistence semantics rather than a mock.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.ai_config import (
    CHANGE_DESCRIPTION_MAX_LENGTH,
    CHANGE_DESCRIPTION_MIN_LENGTH,
    AIConfigurationStore,
    ChangeDescriptionRequiredError,
    validate_change_description,
)
from rag_system.storage import PreconditionFailed


class _FakeStore:
    """In-memory stand-in exposing the CAS primitives the service relies on.

    Binds the real ``create_json`` / ``update_json_cas`` implementations on top
    of an in-memory ``get_json_with_etag`` / ``put_json_conditional`` so the
    create-only and ETag-CAS semantics are exercised for real.
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


# A description within the accepted inclusive 1–500 character window.
_valid_descriptions = st.text(
    min_size=CHANGE_DESCRIPTION_MIN_LENGTH,
    max_size=CHANGE_DESCRIPTION_MAX_LENGTH,
)

# A description outside the window: either empty (too short) or longer than the
# maximum. 501–600 chars keeps generation cheap while covering the over-length
# case just past the boundary.
_invalid_descriptions = st.one_of(
    st.just(""),
    st.text(min_size=CHANGE_DESCRIPTION_MAX_LENGTH + 1, max_size=CHANGE_DESCRIPTION_MAX_LENGTH + 100),
)


# Feature: rag-trust-and-observability, Property 32: AI configuration change validation
# Validates: Requirements 9.3, 9.4
@settings(max_examples=200)
@given(description=_valid_descriptions)
def test_valid_change_description_creates_version_storing_it(description: str) -> None:
    """A 1–500 char description is accepted and stored on a new version (R9.3)."""
    store = AIConfigurationStore(_FakeStore())

    version = store.create_version(
        "cfg",
        prompt="answer the question",
        model="gemini-3.5-flash",
        router_threshold=0.5,
        change_description=description,
    )

    # A new version was created storing exactly the provided description.
    assert version.change_description == description
    history = store.get_history("cfg")
    assert [v.version_id for v in history] == [version.version_id]
    assert store.get_version("cfg", version.version_id) is not None
    assert store.get_version("cfg", version.version_id).change_description == description


# Feature: rag-trust-and-observability, Property 32: AI configuration change validation
# Validates: Requirements 9.3, 9.4
@settings(max_examples=200)
@given(description=_invalid_descriptions)
def test_invalid_change_description_rejected_without_writing(description: str) -> None:
    """An empty/too-long description is rejected with no version written (R9.4)."""
    store = AIConfigurationStore(_FakeStore())
    # Seed a valid existing version so there is an active pointer + history to
    # verify remain unchanged after the rejected create.
    seed = store.create_version(
        "cfg",
        prompt="answer the question",
        model="gemini-3.5-flash",
        router_threshold=0.5,
        change_description="seed configuration",
        version_id="seed",
    )
    store.rollback("cfg", version_id=seed.version_id, operator="op", reason="init")
    active_before = store.get_index("cfg").active_version_id
    history_before = [v.version_id for v in store.get_history("cfg")]

    try:
        store.create_version(
            "cfg",
            prompt="answer the question",
            model="gemini-3.5-flash",
            router_threshold=0.5,
            change_description=description,
            version_id="rejected",
        )
        raised = False
    except ChangeDescriptionRequiredError as exc:
        raised = True
        assert exc.code == "change_description_required"

    assert raised, "expected ChangeDescriptionRequiredError for an out-of-range description"
    # No new version written and the active pointer is unchanged (R9.4).
    assert store.get_version("cfg", "rejected") is None
    assert [v.version_id for v in store.get_history("cfg")] == history_before
    assert store.get_index("cfg").active_version_id == active_before


# Feature: rag-trust-and-observability, Property 32: AI configuration change validation
# Validates: Requirements 9.3, 9.4
@settings(max_examples=200)
@given(description=st.one_of(_valid_descriptions, _invalid_descriptions))
def test_validate_change_description_matches_bounds(description: str) -> None:
    """The standalone validator agrees with the 1–500 inclusive bound."""
    in_range = CHANGE_DESCRIPTION_MIN_LENGTH <= len(description) <= CHANGE_DESCRIPTION_MAX_LENGTH
    if in_range:
        assert validate_change_description(description) == description
    else:
        try:
            validate_change_description(description)
            raised = False
        except ChangeDescriptionRequiredError:
            raised = True
        assert raised
