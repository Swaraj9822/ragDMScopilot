# Implementation Plan

## Overview

This plan fixes CI red caused by two narrowly-scoped test/source drifts: (1) the test
fakes `FakeStore` and `IntegrationStore` only implement the legacy `put_pdf` while the
service now calls `put_raw`, producing 4 pytest failures; and (2) an unused
`RoutingDecision` import in `tests/test_router.py` trips ruff F401. The plan follows the
exploratory bugfix methodology: write a bug-condition exploration test that fails on
unfixed code, capture preservation behavior, apply the test-only fix, then verify the
fix and preservation. All changes are confined to the three test files; no
`src/rag_system/` production code is touched.

## Tasks

- [x] 1. Write bug condition exploration test (BEFORE implementing the fix)
  - **Property 1: Bug Condition** - Test fakes satisfy the current storage interface and lint is clean
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: These are deterministic CI checks, so scope the property to the concrete failing cases rather than generating random inputs:
    - Case A (storage drift): assert that `FakeStore` and `IntegrationStore` each expose a callable `put_raw(document_id, version, filename, content)` attribute, and that invoking the upload code path (the service calling `put_raw`) succeeds for every fake store in the suite. Concretely drive `test_upload_document_queues_ingestion_without_running_pipeline`, `test_upload_worker_query_flow_with_mocked_external_systems`, and `test_update_document_keeps_id_queues_new_version_and_replaces_vectors`.
    - Case B (lint drift): assert that `python -m ruff check .` reports 0 F401 violations for `tests/test_router.py`.
  - Bug Condition reference (from design `isBugCondition`): true when `kind == "pytest"` AND `serviceCallsPutRaw` AND the fake store implements `put_pdf` but not `put_raw`; OR when `kind == "ruff"` AND module is `tests/test_router.py` AND it imports but does not use `RoutingDecision`
  - The test assertions should match the Expected Behavior in Property 1: fakes provide `put_raw(document_id, version, filename, content)`, service calls succeed, affected tests pass, and ruff reports 0 failures
  - Run `python -m pytest` and `python -m ruff check .` on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bug exists)
    - `AttributeError: 'FakeStore' object has no attribute 'put_raw'` (and same for `IntegrationStore`) across the 4 affected tests
    - 1 ruff F401 violation in `tests/test_router.py` line 1 (unused `RoutingDecision`)
  - Document counterexamples found to understand root cause (stale fake-store signatures, leftover unused import after refactor)
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Existing behavior, interfaces, and scope unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe behavior on the UNFIXED code for non-buggy inputs (cases where `isBugCondition` returns false):
    - The 50 currently-passing tests pass (`python -m pytest` → 50 passed, 4 failed)
    - The router tests in `tests/test_router.py` that use `QueryRoute` and `_parse_routing_response` pass
    - The integration test's legacy `get_pdf` / `raw_pdf_key` usage behaves as-is
    - No files under `src/rag_system/` are modified (production storage/service interfaces `put_raw`, `get_raw`, `put_pdf`/`get_pdf` aliases unchanged)
  - Write property-based tests capturing observed behavior patterns from the Preservation Requirements:
    - For `IntegrationStore`, generate random `(document_id, version, filename, content)` tuples and assert a `put_raw` → `get_raw` round-trip returns the original bytes and produces keys consistent with production `raw_document_key` (suffix/extension preservation across the input domain, including non-PDF filenames)
    - Assert the still-used router imports (`QueryRoute`, `_parse_routing_response`) remain importable and used
  - Property-based testing generates many test cases for stronger guarantees that the fake faithfully mirrors the production interface
  - Run tests on UNFIXED code (the preservation round-trip PBT targets the post-fix fake; the regression baseline is the existing 50 passing tests plus ruff observed before the fix)
  - **EXPECTED OUTCOME**: The 50 previously-passing tests and the router imports PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and the baseline is recorded
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix for CI test drift (test fakes missing `put_raw`, plus unused import)

  - [x] 3.1 Add `put_raw` to `FakeStore` in `tests/test_api_ingestion_queue.py`
    - Add `put_raw(self, document_id, version, filename, content) -> str` matching the production signature
    - Record the upload into `self.uploads` so `len(store.uploads) == 1` continues to hold
    - Return an `s3://bucket/raw/...` shaped URI so `body["s3_uri"].startswith("s3://bucket/raw/")` holds
    - Keep legacy `put_pdf` as a thin alias delegating to `put_raw` (lower-risk choice)
    - _Bug_Condition: isBugCondition(input) where kind == "pytest" and fake implements put_pdf but not put_raw_
    - _Expected_Behavior: fake provides put_raw(document_id, version, filename, content); service call succeeds; affected tests pass (Property 1 / expectedBehavior from design)_
    - _Preservation: Preservation Requirements from design - self.uploads bookkeeping and s3 URI shape unchanged_
    - _Requirements: 2.1, 2.2_

  - [x] 3.2 Add `put_raw` and matching `get_raw` to `IntegrationStore` in `tests/test_rag_flow_integration.py`
    - Add `put_raw(self, document_id, version, filename, content) -> str` storing bytes under `raw_document_key(document_id, version, filename)`
    - Add `get_raw(self, document_id, version, filename) -> bytes` reading the same key
    - Update the import from `rag_system.storage` to include `raw_document_key` (keeping `raw_pdf_key`/`get_pdf` if still referenced) so keys stay consistent
    - Preserve existing `put_pdf`/`get_pdf` (or re-express in terms of `put_raw`/`get_raw`) without changing observable behavior
    - _Bug_Condition: isBugCondition(input) where kind == "pytest" and IntegrationStore implements put_pdf but not put_raw_
    - _Expected_Behavior: fake provides put_raw/get_raw matching production interface; upload → worker → query flow succeeds (Property 1 / expectedBehavior from design)_
    - _Preservation: Preservation Requirements - legacy get_pdf/raw_pdf_key paths intact_
    - _Requirements: 2.1, 2.2_

  - [x] 3.3 Remove the unused `RoutingDecision` import in `tests/test_router.py`
    - Change line 1 from `from rag_system.router import QueryRoute, RoutingDecision, _parse_routing_response` to `from rag_system.router import QueryRoute, _parse_routing_response`
    - Drop only `RoutingDecision`, leaving the still-used `QueryRoute` and `_parse_routing_response` intact
    - _Bug_Condition: isBugCondition(input) where kind == "ruff" and module imports but does not use RoutingDecision_
    - _Expected_Behavior: ruff reports 0 F401 failures for tests/test_router.py (Property 1 / expectedBehavior from design)_
    - _Preservation: Preservation Requirements - QueryRoute and _parse_routing_response imports and the six router tests unchanged_
    - _Requirements: 2.3, 3.3_

  - [x] 3.4 Verify the bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Test fakes satisfy the current storage interface and lint is clean
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior; when it passes it confirms the expected behavior is satisfied
    - Run the bug condition exploration test from step 1 (`python -m pytest` for the 4 affected tests and `python -m ruff check .`)
    - **EXPECTED OUTCOME**: Test PASSES - 54 tests passing, 0 ruff failures (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Existing behavior, interfaces, and scope unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from step 2 plus the full suite
    - Confirm `src/rag_system/` is byte-for-byte unchanged (git diff limited to the three test files)
    - Confirm the six router tests still pass and the legacy `get_pdf`/`raw_pdf_key` paths behave as before
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions across the 50 previously-passing tests)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 4. Checkpoint - Ensure all tests pass
  - Final gate: `python -m pytest` reports 54 passed / 0 failed and `python -m ruff check .` reports 0 failures
  - Confirm the diff is limited to the three test files (no `src/rag_system/` changes, no out-of-scope items)
  - Ensure all tests pass, ask the user if questions arise
  - _Requirements: 2.2, 2.3, 3.1, 3.2, 3.4_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "dependsOn": [] },
    { "wave": 2, "tasks": ["2"], "dependsOn": ["1"] },
    { "wave": 3, "tasks": ["3.1", "3.2", "3.3"], "dependsOn": ["2"] },
    { "wave": 4, "tasks": ["3.4", "3.5"], "dependsOn": ["3.1", "3.2", "3.3"] },
    { "wave": 5, "tasks": ["4"], "dependsOn": ["3.4", "3.5"] }
  ]
}
```

Tasks 1 and 2 must be completed before Task 3 (write tests before the fix). Subtasks
3.1–3.3 are independent of each other and can be done in any order, but 3.4 and 3.5
depend on all three. Task 4 is the final gate after 3.4 and 3.5.

## Notes

- Tasks 1 and 2 are STANDALONE property-based test tasks written BEFORE the fix.
- Task 1's test MUST FAIL on unfixed code (confirms the bug); do not "fix" the test when
  it fails. The same test validates the fix in 3.4.
- Task 2's tests MUST PASS on unfixed code (establishes the preservation baseline).
- The fix is intentionally test-only: edits are limited to
  `tests/test_api_ingestion_queue.py`, `tests/test_rag_flow_integration.py`, and
  `tests/test_router.py`. No file under `src/rag_system/` is modified.
- Out of scope (must remain untouched): Docker, readiness probes, metadata store, and
  any other broader production-readiness items.
- Final success criteria: `python -m pytest` → 54 passed / 0 failed and
  `python -m ruff check .` → 0 failures.
