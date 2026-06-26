"""Bug condition exploration test for the CI test-drift defects.

Property 1: Bug Condition - Test fakes satisfy the current storage interface and
lint is clean.

This is a BUGFIX exploration test. It encodes the EXPECTED (fixed) behavior and is
therefore EXPECTED TO FAIL on the current unfixed code. Each failure is a
counterexample that confirms the bug exists:

  * Case A (storage drift): the service persists uploads via
    ``put_raw(document_id, version, filename, content)``, but the in-test fakes
    ``FakeStore`` (tests/test_api_ingestion_queue.py) and ``IntegrationStore``
    (tests/test_rag_flow_integration.py) only implement the legacy
    ``put_pdf``. Driving the upload code path raises
    ``AttributeError: '<Fake>' object has no attribute 'put_raw'``.

  * Case B (lint drift): tests/test_router.py imports ``RoutingDecision`` on
    line 1 but never uses it, so ruff reports an F401 unused-import violation.

Bug Condition reference (design isBugCondition):
  * kind == "pytest" AND serviceCallsPutRaw AND fake implements put_pdf but not put_raw
  * OR kind == "ruff" AND module == tests/test_router.py AND imports but does not
    use RoutingDecision

Because these are deterministic CI checks, the property is scoped to the concrete
failing cases rather than randomized inputs.

**Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3**
"""

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

# Sibling test modules are importable by basename under pytest's prepend import mode.
# Import the modules (not the test functions directly) so pytest does not re-collect
# the affected tests as duplicates inside this exploration module.
import test_api_ingestion_queue as api_queue_tests
import test_rag_flow_integration as integration_tests

FakeStore = api_queue_tests.FakeStore
IntegrationStore = integration_tests.IntegrationStore

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fake stores that must mirror the current production storage interface.
FAKE_STORES = [FakeStore, IntegrationStore]

# The required production storage method signature parameters (after ``self``).
REQUIRED_PUT_RAW_PARAMS = ["document_id", "version", "filename", "content"]


@pytest.mark.parametrize("fake_store_cls", FAKE_STORES, ids=lambda c: c.__name__)
def test_fake_store_exposes_put_raw_matching_production(fake_store_cls) -> None:
    """Case A: every fake store exposes a callable ``put_raw`` matching production.

    EXPECTED ON UNFIXED CODE: FAILS - the fakes only define ``put_pdf``, so
    ``put_raw`` is absent (counterexample for the storage-drift bug condition).
    """
    put_raw = getattr(fake_store_cls, "put_raw", None)
    assert put_raw is not None, (
        f"{fake_store_cls.__name__} has no 'put_raw' attribute - it only implements "
        f"the legacy 'put_pdf', so service calls to put_raw raise AttributeError"
    )
    assert callable(put_raw), f"{fake_store_cls.__name__}.put_raw is not callable"

    params = [p for p in inspect.signature(put_raw).parameters if p != "self"]
    assert params == REQUIRED_PUT_RAW_PARAMS, (
        f"{fake_store_cls.__name__}.put_raw signature {params!r} does not match the "
        f"production storage interface {REQUIRED_PUT_RAW_PARAMS!r}"
    )


def test_upload_code_path_succeeds_for_fake_store(monkeypatch) -> None:
    """Case A: driving the FakeStore upload code path (service calls put_raw) succeeds.

    EXPECTED ON UNFIXED CODE: FAILS with
    AttributeError: 'FakeStore' object has no attribute 'put_raw'.
    """
    api_queue_tests.test_upload_document_queues_ingestion_without_running_pipeline(
        monkeypatch
    )


def test_upload_worker_query_flow_succeeds_for_integration_store(monkeypatch) -> None:
    """Case A: driving the IntegrationStore upload -> worker -> query flow succeeds.

    EXPECTED ON UNFIXED CODE: FAILS with
    AttributeError: 'IntegrationStore' object has no attribute 'put_raw'.
    """
    integration_tests.test_upload_worker_query_flow_with_mocked_external_systems(
        monkeypatch
    )


def test_update_flow_succeeds_for_integration_store(monkeypatch) -> None:
    """Case A: driving the IntegrationStore update (second put_raw) flow succeeds.

    EXPECTED ON UNFIXED CODE: FAILS with
    AttributeError: 'IntegrationStore' object has no attribute 'put_raw'.
    """
    integration_tests.test_update_document_keeps_id_queues_new_version_and_replaces_vectors(
        monkeypatch
    )


def test_router_module_has_no_f401_unused_import() -> None:
    """Case B: ruff reports 0 F401 violations for tests/test_router.py.

    EXPECTED ON UNFIXED CODE: FAILS - RoutingDecision is imported but unused,
    producing exactly one F401 violation.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "tests/test_router.py",
            "--select",
            "F401",
            "--output-format",
            "concise",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    f401_lines = [line for line in combined.splitlines() if "F401" in line]
    assert result.returncode == 0 and not f401_lines, (
        "ruff reported F401 unused-import violation(s) for tests/test_router.py:\n"
        + "\n".join(f401_lines)
    )
