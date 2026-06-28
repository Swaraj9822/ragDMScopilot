# CI Test Drift Fix Bugfix Design

## Overview

CI is red because of two independent, narrowly-scoped drifts between the production
code and the test suite:

1. **Storage interface drift.** `RagService._queue_document` persists raw uploads by
   calling `self._store.put_raw(document_id, version, filename, content)`. The real
   store (`S3ArtifactStore` in `src/rag_system/storage.py`) implements `put_raw` and
   keeps a backward-compatible `put_pdf` alias. The in-test fakes — `FakeStore` in
   `tests/test_api_ingestion_queue.py` and `IntegrationStore` in
   `tests/test_rag_flow_integration.py` — only implement the old
   `put_pdf(document_id, version, content)` signature. When the service calls
   `put_raw` on these fakes, Python raises `AttributeError`, producing 4 pytest
   failures.

2. **Lint drift.** `tests/test_router.py` imports `RoutingDecision` on line 1 but
   never references it, so ruff flags it as an unused import (F401).

The fix strategy is deliberately minimal: align the two test fakes with the current
`put_raw` storage interface and remove the unused import. No production code changes,
and nothing outside the CI/test-drift scope (Docker, readiness probes, metadata
store) is touched. Success means 54 tests passing and ruff reporting 0 failures,
with the 50 currently-passing tests unaffected.

## Glossary

- **Bug_Condition (C)**: The condition that triggers a bug — either (a) the test
  suite exercising a code path where the service calls `put_raw` on a fake store that
  only implements `put_pdf`, or (b) ruff analyzing `tests/test_router.py` with the
  unused `RoutingDecision` import present.
- **Property (P)**: The desired behavior — fakes expose a
  `put_raw(document_id, version, filename, content)` method so the call succeeds and
  the affected tests pass; and ruff reports zero F401 violations.
- **Preservation**: The 50 currently-passing tests, the production storage/service
  interfaces, the still-used router-test imports (`QueryRoute`,
  `_parse_routing_response`), and the CI/test-drift scope boundary — all must remain
  unchanged.
- **put_raw**: The current storage method `put_raw(document_id, version, filename, content) -> str`
  on `S3ArtifactStore`, used by `RagService._queue_document` to persist uploads.
- **put_pdf**: The legacy storage method `put_pdf(document_id, version, content)` that
  predates `put_raw`; retained as a backward-compatible alias in production storage,
  but the only method the test fakes currently implement.
- **FakeStore**: The lightweight store stub in `tests/test_api_ingestion_queue.py`.
- **IntegrationStore**: The in-memory store stub in `tests/test_rag_flow_integration.py`.
- **RoutingDecision**: A model class in `src/rag_system/router.py`, imported but
  unused by `tests/test_router.py`.

## Bug Details

### Bug Condition

The bug manifests in two distinct situations. For the storage drift, it occurs
whenever a test drives a service code path that persists a raw upload (the `POST
/documents` flow that calls `RagService._queue_document`, which in turn calls
`self._store.put_raw(...)`) while the store under test is a fake that only defines
`put_pdf`. For the lint drift, it occurs whenever ruff statically analyzes
`tests/test_router.py` while the unused `RoutingDecision` import is present.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type CiCheck
         (either a pytest invocation against a fake store,
          or a ruff analysis of a test module)
  OUTPUT: boolean

  // Case A: storage interface drift
  IF input.kind == "pytest"
     AND input.serviceCallsPutRaw == true
     AND input.fakeStore.implements("put_raw") == false
     AND input.fakeStore.implements("put_pdf") == true
  THEN RETURN true

  // Case B: unused-import lint drift
  IF input.kind == "ruff"
     AND input.module == "tests/test_router.py"
     AND input.imports("RoutingDecision") == true
     AND input.uses("RoutingDecision") == false
  THEN RETURN true

  RETURN false
END FUNCTION
```

### Examples

- **Battle of the signatures (FakeStore):** `test_upload_document_queues_ingestion_without_running_pipeline`
  posts a PDF; the service calls `store.put_raw(document_id, version, "source.pdf", content)`;
  `FakeStore` has no `put_raw`, so the request handling raises `AttributeError`.
  Expected: HTTP 202 with a queued record and one recorded upload. Actual: failure.
- **Integration upload (IntegrationStore):** `test_upload_worker_query_flow_with_mocked_external_systems`
  posts `report.pdf`; the service calls `put_raw(...)`; `IntegrationStore` only has
  `put_pdf`, so the upload fails before the worker/query flow runs.
  Expected: full upload → worker → query flow succeeds. Actual: failure at upload.
- **Update flow (IntegrationStore):** `test_update_document_keeps_id_queues_new_version_and_replaces_vectors`
  triggers a second `put_raw` for the new version; fails for the same reason.
- **Lint (test_router.py):** `python -m ruff check .` reports F401 — `RoutingDecision`
  imported on line 1 but never used. Expected: 0 ruff failures. Actual: 1 failure.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The 50 currently-passing tests SHALL continue to pass unchanged.
- The production storage interface (`put_raw`, `get_raw`, and the backward-compatible
  `put_pdf`/`get_pdf` aliases) and the service interface SHALL remain unchanged — no
  edits to any file under `src/rag_system/`.
- The still-used router-test imports `QueryRoute` and `_parse_routing_response`, and
  the six router tests that depend on them, SHALL continue to work unchanged.
- The behavior of `put_pdf`/`get_pdf` and the `raw_pdf_key` helper used elsewhere in
  the integration test SHALL remain intact (the fix adds `put_raw`/`get_raw`; it must
  not break the existing legacy paths).

**Scope:**
All inputs that do NOT match the bug condition should be completely unaffected by this
fix. This includes:
- Production application code under `src/rag_system/` (storage, service, router, etc.).
- Tests and code paths that do not involve the `put_pdf`/`put_raw` drift.
- The remaining imports and assertions in `tests/test_router.py`.
- Broader production-readiness items explicitly out of scope: Docker, readiness probes,
  metadata store, and any infrastructure changes.

**Note:** The expected correct behavior for buggy inputs is defined in the Correctness
Properties section (Property 1). This section focuses on what must NOT change.

## Hypothesized Root Cause

Based on the bug analysis, the causes are well-localized and high-confidence:

1. **Stale fake-store signature (FakeStore)**: `FakeStore.put_pdf(document_id, version, content)`
   was written against the pre-`put_raw` storage interface. The service was later
   migrated to `put_raw(document_id, version, filename, content)`, but this fake was
   not updated, so the method the service calls does not exist on the fake.

2. **Stale fake-store signature (IntegrationStore)**: Same root cause as above —
   `IntegrationStore` only defines `put_pdf` (and `get_pdf`), keyed via `raw_pdf_key`.
   The service's `put_raw` call has no matching method. The worker's read path
   (`get_raw` with a fallback to `get_pdf`) still works via the legacy fallback, but
   the write path fails first.

3. **Unused import left after refactor (test_router.py)**: `RoutingDecision` was
   likely imported when the test referenced it directly. The tests now assert via
   `decision.route` (a `QueryRoute`) and call `_parse_routing_response`, so the
   `RoutingDecision` name is dead and trips ruff's F401 rule.

## Correctness Properties

Property 1: Bug Condition - Test fakes satisfy the current storage interface and lint is clean

_For any_ input where the bug condition holds (isBugCondition returns true), the fixed
test suite SHALL behave correctly: each fake store (`FakeStore`, `IntegrationStore`)
SHALL provide a `put_raw(document_id, version, filename, content)` method matching the
production storage interface so that service calls succeed and the affected tests pass;
and ruff SHALL report 0 failures for `tests/test_router.py` because the unused
`RoutingDecision` import is removed. As a result, `python -m pytest` reports all 54
tests passing and `python -m ruff check .` reports 0 failures.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Existing behavior, interfaces, and scope unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns false),
the fixed code SHALL produce the same result as the original code, preserving: the 50
previously-passing tests, the production storage/service interfaces (`put_raw`,
`get_raw`, `put_pdf`/`get_pdf` aliases) with no changes under `src/rag_system/`, the
still-used router-test imports (`QueryRoute`, `_parse_routing_response`) and their
tests, and the CI/test-drift scope boundary (no Docker, readiness probes, or metadata
store changes).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct, three small, test-only edits are needed.

**File**: `tests/test_api_ingestion_queue.py`

**Class**: `FakeStore`

**Specific Changes**:
1. **Add `put_raw` matching the production signature**: Add
   `put_raw(self, document_id, version, filename, content) -> str` that records the
   upload (preserving the existing `self.uploads` bookkeeping and the
   `s3://bucket/raw/...` return shape the test asserts on, e.g.
   `body["s3_uri"].startswith("s3://bucket/raw/")`).
   - Keep recording into `self.uploads` so `len(store.uploads) == 1` continues to hold.
   - The legacy `put_pdf` may be kept as a thin alias delegating to `put_raw`, or
     removed if unused; keeping it as an alias is the lower-risk choice.

**File**: `tests/test_rag_flow_integration.py`

**Class**: `IntegrationStore`

**Specific Changes**:
2. **Add `put_raw` (and a matching `get_raw`)**: Add
   `put_raw(self, document_id, version, filename, content) -> str` that stores bytes
   under `raw_document_key(document_id, version, filename)`, and a corresponding
   `get_raw(self, document_id, version, filename) -> bytes` reading the same key, so
   the upload → worker read path uses the current interface directly rather than
   relying on the legacy `get_pdf` fallback.
   - Update the import from `rag_system.storage` to include `raw_document_key`
     (alongside or instead of `raw_pdf_key`, keeping `raw_pdf_key`/`get_pdf` if still
     referenced) so keys stay consistent.
   - Preserve the existing `put_pdf`/`get_pdf` methods or re-express them in terms of
     `put_raw`/`get_raw` without changing observable behavior.

**File**: `tests/test_router.py`

**Specific Changes**:
3. **Remove the unused import**: Change the line-1 import from
   `from rag_system.router import QueryRoute, RoutingDecision, _parse_routing_response`
   to `from rag_system.router import QueryRoute, _parse_routing_response`, dropping
   only `RoutingDecision` and leaving the still-used symbols intact.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that
demonstrate the bug on the unfixed code (the 4 failing tests and the 1 ruff failure),
then verify the fix makes CI green while preserving all previously-passing behavior.
Because the entire surface area is the test suite plus a lint check, the primary
"tests" here are the existing pytest cases and the ruff run themselves.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix
and confirm the root cause. If the counterexamples do not match the hypothesis, we
re-hypothesize.

**Test Plan**: Run `python -m pytest` and `python -m ruff check .` against the UNFIXED
code and observe the failures and their messages.

**Test Cases**:
1. **FakeStore upload test**: `test_upload_document_queues_ingestion_without_running_pipeline`
   in `tests/test_api_ingestion_queue.py` (will fail on unfixed code — `AttributeError: 'FakeStore' object has no attribute 'put_raw'`).
2. **IntegrationStore upload/worker/query flow**: `test_upload_worker_query_flow_with_mocked_external_systems`
   (will fail on unfixed code at the upload step).
3. **IntegrationStore delete flow**: `test_delete_document_marks_deleted_and_removes_vectors`
   (will fail on unfixed code — depends on a successful upload via `put_raw`).
4. **IntegrationStore update flow**: `test_update_document_keeps_id_queues_new_version_and_replaces_vectors`
   (will fail on unfixed code at the second `put_raw`).
5. **Ruff F401**: `python -m ruff check .` (will report 1 failure — unused `RoutingDecision`).

**Expected Counterexamples**:
- `AttributeError` for the missing `put_raw` method on the fake stores in the 4 tests.
- One ruff F401 violation in `tests/test_router.py` line 1.
- Possible causes: stale fake-store signatures, leftover unused import after a refactor.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed code
produces the expected behavior (tests pass, ruff clean).

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := runFixedCheck(input)
  ASSERT expectedBehavior(result)
  // pytest: the 4 previously-failing tests now pass
  // ruff: 0 F401 violations for tests/test_router.py
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed
code produces the same result as the original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalBehavior(input) = fixedBehavior(input)
  // the 50 previously-passing tests still pass
  // src/rag_system/ is byte-for-byte unchanged
  // QueryRoute / _parse_routing_response imports and router tests unchanged
END FOR
```

**Testing Approach**: For this bugfix the dominant preservation signal is the full
existing test suite plus ruff. Property-based testing is recommended where it adds
value (see below) because it generates many inputs across the storage-key domain and
guards against accidental divergence between the fake `put_raw`/`get_raw` round-trip
and the production `raw_document_key` scheme. The strongest preservation guarantee,
though, is that no file under `src/rag_system/` is modified.

**Test Plan**: Observe behavior on the UNFIXED code for the non-buggy inputs (the 50
passing tests and the still-used router imports), apply the fix, then re-run the full
suite and ruff to confirm nothing changed for those inputs.

**Test Cases**:
1. **Full suite regression**: Confirm the 50 previously-passing tests still pass after
   the fix (`python -m pytest` → 54 passed, 0 failed).
2. **Production interface untouched**: Confirm `src/rag_system/storage.py` and
   `src/rag_system/service.py` are unchanged (git diff limited to the three test files).
3. **Router imports preserved**: Confirm all six tests in `tests/test_router.py` still
   pass and that `QueryRoute` / `_parse_routing_response` remain imported and used.
4. **Legacy alias preserved**: Confirm the integration test's `get_pdf`/`raw_pdf_key`
   usage still behaves as before (no observable change to legacy paths).

### Unit Tests

- The existing `FakeStore`-based tests in `tests/test_api_ingestion_queue.py` exercise
  the upload path and assert on `store.uploads` and the `s3://bucket/raw/...` URI.
- The existing `tests/test_router.py` tests exercise routing-response parsing.
- Edge cases: oversized-upload rejection tests must still short-circuit before any
  `put_raw` call (they assert `store.uploads == []`).

### Property-Based Tests

- For the `IntegrationStore`, optionally generate random `(document_id, version,
  filename, content)` tuples and assert a `put_raw` → `get_raw` round-trip returns the
  original bytes and produces keys consistent with production `raw_document_key`,
  confirming the fake faithfully mirrors the production interface.
- Generate non-PDF filenames/extensions to confirm key derivation matches
  `raw_document_key` behavior (suffix preservation) across the input domain.

### Integration Tests

- The existing `test_upload_worker_query_flow_with_mocked_external_systems` provides
  end-to-end coverage: upload (`put_raw`) → worker read (`get_raw`/`get_pdf` fallback)
  → query, validating the fake aligns with the full service flow.
- `test_delete_document_marks_deleted_and_removes_vectors` and
  `test_update_document_keeps_id_queues_new_version_and_replaces_vectors` cover context
  transitions (delete and re-upload of a new version) through the fixed `put_raw` path.
- Final gate: `python -m pytest` reports 54 passed / 0 failed and
  `python -m ruff check .` reports 0 failures.
