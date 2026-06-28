# Bugfix Requirements Document

## Introduction

A production-readiness review identified that CI is red due to two independent
defects caused by drift between the source code and the test suite:

1. **Storage interface drift (4 pytest failures).** The service layer in
   `src/rag_system/service.py` now persists raw uploads via the storage method
   `put_raw(document_id, version, filename, content)`. The real storage layer
   (`src/rag_system/storage.py`, `S3ArtifactStore`) exposes both `put_raw` and a
   backward-compatible `put_pdf` alias. However, the fake/mock stores used in the
   test suite (`FakeStore` in `tests/test_api_ingestion_queue.py` and
   `IntegrationStore` in `tests/test_rag_flow_integration.py`) only implement the
   old `put_pdf` method. When the service calls `put_raw` on these fakes, the call
   fails because the method does not exist, producing 4 test failures.

2. **Lint failure (ruff).** `tests/test_router.py` imports `RoutingDecision` on
   line 1 but never uses it, which ruff flags as an unused import (F401).

The goal of this bugfix is to make CI green — all tests passing and ruff clean —
by aligning the test fakes with the current `put_raw` storage interface and
removing the unused import. The fix must not regress any of the 50 currently
passing tests, and must stay within the CI/test-drift scope (it must not touch
the broader production-readiness items such as Docker, readiness probes, or a
metadata store).

### Verification Baseline (from review)

- `python -m pytest`: 50 passed, 4 failed (all 4 caused by the `put_pdf`/`put_raw` drift).
- `python -m ruff check .`: 1 failure (unused `RoutingDecision` import).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the service queues a document under test and calls `put_raw` on a fake store that only implements `put_pdf` THEN the call fails with an `AttributeError` (no `put_raw` method), causing the affected tests in `tests/test_api_ingestion_queue.py` and `tests/test_rag_flow_integration.py` to fail (4 failures total)
1.2 WHEN `python -m pytest` is run on the current codebase THEN the system reports 50 passed and 4 failed
1.3 WHEN `python -m ruff check .` is run with the unused `RoutingDecision` import present in `tests/test_router.py` line 1 THEN ruff reports 1 failure (unused import, F401)

### Expected Behavior (Correct)

2.1 WHEN the service queues a document under test and calls `put_raw` on a fake store THEN the fake store SHALL provide a `put_raw(document_id, version, filename, content)` method matching the current storage interface, and the call SHALL succeed
2.2 WHEN `python -m pytest` is run on the fixed codebase THEN the system SHALL report all 54 tests passing with 0 failures
2.3 WHEN `python -m ruff check .` is run on the fixed codebase THEN ruff SHALL report 0 failures (the unused `RoutingDecision` import removed from `tests/test_router.py`)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the 50 currently passing tests are run after the fix THEN the system SHALL CONTINUE TO pass all of them
3.2 WHEN production code in `src/rag_system/` is considered THEN the fix SHALL CONTINUE TO leave the production storage and service interfaces (`put_raw`, `get_raw`, and the backward-compatible `put_pdf`/`get_pdf` aliases) unchanged
3.3 WHEN the remaining symbols imported in `tests/test_router.py` (`QueryRoute`, `_parse_routing_response`) are used by the router tests THEN those imports and tests SHALL CONTINUE TO work unchanged
3.4 WHEN scope is considered THEN the fix SHALL CONTINUE TO exclude broader production-readiness items (Docker, readiness probes, metadata store, etc.), changing only the test fakes and the unused import
