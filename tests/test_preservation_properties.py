"""Preservation property tests for the CI test-drift bugfix.

Property 2: Preservation - Existing behavior, interfaces, and scope unchanged.

This module follows the observation-first methodology. It captures behavior that
must NOT change once the fix (Task 3) lands, and it is split into two clearly
separated sections:

  (a) BASELINE assertions that PASS on the *current, unfixed* code. These record
      the behavior to preserve: the production storage key scheme
      (``raw_document_key`` / ``raw_pdf_key``), the legacy ``put_pdf`` / ``get_pdf``
      round-trip on ``IntegrationStore``, the production ``S3ArtifactStore``
      interface, and the still-used router-test imports (``QueryRoute``,
      ``_parse_routing_response``).

  (b) POST-FIX-TARGETED round-trip property test. ``IntegrationStore.put_raw`` /
      ``get_raw`` do NOT exist yet - they are added in Task 3. This property
      therefore EXPECTS TO FAIL on unfixed code (``AttributeError``) and will pass
      once Task 3 adds the methods. It uses the production ``raw_document_key``
      helper as the source of truth for key derivation, so the fake is proven to
      faithfully mirror the production interface across the input domain
      (including non-PDF filenames / suffix preservation).

Bug Condition reference (design ``isBugCondition``): this module exercises inputs
where ``isBugCondition`` is FALSE (legacy paths, production helpers, router
imports), so they must be unaffected by the fix.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**
"""

import inspect
import string
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from rag_system.storage import S3ArtifactStore, raw_document_key, raw_pdf_key

# IntegrationStore is the fake whose put_raw/get_raw round-trip we preserve/extend.
import test_rag_flow_integration as integration_tests

IntegrationStore = integration_tests.IntegrationStore


# ---------------------------------------------------------------------------
# Smart generators - constrained to the realistic key/upload input domain.
# ---------------------------------------------------------------------------

# Document ids and versions are interpolated into S3 key paths; keep them to a
# safe, non-empty identifier alphabet so generated keys stay well-formed.
_identifiers = st.text(
    alphabet=string.ascii_letters + string.digits + "-_",
    min_size=1,
    max_size=24,
)

# A base filename stem plus an extension drawn from a mix of PDF and non-PDF
# suffixes (and the empty/no-extension case) to exercise suffix preservation
# across the whole input domain, per the Preservation Requirements.
_filename_stems = st.text(
    alphabet=string.ascii_letters + string.digits + "-_",
    min_size=1,
    max_size=24,
)
_extensions = st.sampled_from(
    [".pdf", ".txt", ".docx", ".csv", ".bin", ".PNG", ".json", ".tar.gz", ""]
)
_filenames = st.builds(lambda stem, ext: f"{stem}{ext}", _filename_stems, _extensions)

_contents = st.binary(min_size=0, max_size=512)


# ===========================================================================
# (a) BASELINE assertions - these PASS on the current unfixed code.
#     They record the behavior that must be preserved by the fix.
# ===========================================================================


@given(document_id=_identifiers, version=_identifiers, filename=_filenames)
def test_baseline_production_raw_document_key_preserves_suffix(
    document_id: str, version: str, filename: str
) -> None:
    """Source-of-truth: production ``raw_document_key`` preserves the file suffix.

    PASSES on unfixed code - documents the key-derivation contract the fake must
    mirror once ``put_raw`` is added (including non-PDF and no-extension names).
    """
    key = raw_document_key(document_id, version, filename)
    expected_suffix = Path(filename).suffix or ".bin"
    assert key == f"raw/{document_id}/{version}/source{expected_suffix}"


@given(document_id=_identifiers, version=_identifiers, content=_contents)
def test_baseline_integration_store_legacy_put_pdf_get_pdf_round_trip(
    document_id: str, version: str, content: bytes
) -> None:
    """Legacy ``put_pdf`` -> ``get_pdf`` round-trip on the fake is unchanged.

    PASSES on unfixed code - the fix must not break the legacy alias path or its
    ``raw_pdf_key`` key derivation.
    """
    store = IntegrationStore()
    uri = store.put_pdf(document_id, version, content)
    assert uri == f"s3://bucket/{raw_pdf_key(document_id, version)}"
    assert store.get_pdf(document_id, version) == content


def test_baseline_production_storage_interface_present() -> None:
    """Production ``S3ArtifactStore`` keeps put_raw/get_raw and put_pdf/get_pdf.

    PASSES on unfixed code - the fix is test-only; the production interface under
    ``src/rag_system/`` must remain unchanged.
    """
    put_raw_params = [
        p for p in inspect.signature(S3ArtifactStore.put_raw).parameters if p != "self"
    ]
    assert put_raw_params == ["document_id", "version", "filename", "content"]

    get_raw_params = [
        p for p in inspect.signature(S3ArtifactStore.get_raw).parameters if p != "self"
    ]
    assert get_raw_params == ["document_id", "version", "filename"]

    # Backward-compatible aliases remain available.
    assert callable(S3ArtifactStore.put_pdf)
    assert callable(S3ArtifactStore.get_pdf)


def test_baseline_router_symbols_importable_and_used() -> None:
    """Still-used router imports remain importable and functional.

    PASSES on unfixed code - ``QueryRoute`` and ``_parse_routing_response`` (the
    symbols kept after the F401 fix) must continue to work unchanged.
    """
    from rag_system.router import QueryRoute, _parse_routing_response

    decision = _parse_routing_response(
        '{"route": "rag", "reasoning": "preservation check", "confidence": 0.9}'
    )
    assert decision.route == QueryRoute.rag
    assert decision.confidence == 0.9


# ===========================================================================
# (b) POST-FIX-TARGETED property test - EXPECTED TO FAIL until Task 3.
#     IntegrationStore.put_raw / get_raw do not exist yet; this property proves
#     the fake faithfully mirrors the production raw_document_key scheme once
#     the methods are added.
# ===========================================================================


@given(
    document_id=_identifiers,
    version=_identifiers,
    filename=_filenames,
    content=_contents,
)
def test_integration_store_put_raw_get_raw_round_trip(
    document_id: str, version: str, filename: str, content: bytes
) -> None:
    """POST-FIX: ``put_raw`` -> ``get_raw`` round-trips and matches production keys.

    EXPECTED ON UNFIXED CODE: FAILS - ``IntegrationStore`` only implements the
    legacy ``put_pdf``/``get_pdf``; ``put_raw``/``get_raw`` are added in Task 3.
    Once added, the round-trip must return the original bytes and the returned URI
    must match the production ``raw_document_key`` derivation (suffix preserved
    for non-PDF filenames too).
    """
    store = IntegrationStore()
    uri = store.put_raw(document_id, version, filename, content)

    expected_key = raw_document_key(document_id, version, filename)
    assert uri == f"s3://bucket/{expected_key}"
    assert store.get_raw(document_id, version, filename) == content
