"""Unit tests for not-found and empty-result API responses (task 14.6).

Exercises the four "nothing matched" code paths of the Trace_Query_Service and
Log_Query_Service HTTP endpoints registered on the existing FastAPI app:

* ``GET /traces/{trace_id}`` returns HTTP 404 (and *only* 404) when no trace
  exists for a syntactically valid trace_id (R7.4).
* ``GET /traces`` returns an empty result set with HTTP 200 when a valid search
  matches no traces (R8.10).
* ``GET /logs/{trace_id}`` returns an empty result set with HTTP 200 when a valid
  trace_id has no correlated log records (R15.4).
* ``GET /logs`` returns an empty result set with HTTP 200 when a valid search
  matches no log records (R16.9).

The stores are replaced with in-process fakes injected through ``get_trace_store``
and ``get_log_store`` so no live PostgreSQL is required.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.observability_tracing.log_store import LogSearchFilters
from rag_system.observability_tracing.models import LogRecordModel, Trace
from rag_system.observability_tracing.trace_store import TraceSearchFilters

VALID_TRACE_ID = "0123456789abcdef0123456789abcdef"


class EmptyTraceStore:
    """Trace store double that never finds a trace and never matches a search."""

    def __init__(self) -> None:
        self.get_trace_calls: list[str] = []
        self.search_filters: list[TraceSearchFilters] = []

    def get_trace(self, trace_id: str) -> Trace | None:
        self.get_trace_calls.append(trace_id)
        return None

    def search_traces(self, filters: TraceSearchFilters) -> list[Trace]:
        self.search_filters.append(filters)
        return []


class EmptyLogStore:
    """Log store double that returns no records for any query."""

    def __init__(self) -> None:
        self.get_by_trace_calls: list[str] = []
        self.search_filters: list[LogSearchFilters] = []

    def get_by_trace(self, trace_id: str) -> list[LogRecordModel]:
        self.get_by_trace_calls.append(trace_id)
        return []

    def search(self, filters: LogSearchFilters) -> list[LogRecordModel]:
        self.search_filters.append(filters)
        return []


def _client(monkeypatch, *, trace_store=None, log_store=None) -> TestClient:
    if trace_store is not None:
        monkeypatch.setattr(api_module, "get_trace_store", lambda: trace_store)
    if log_store is not None:
        monkeypatch.setattr(api_module, "get_log_store", lambda: log_store)
    return TestClient(api_module.app)


# -- GET /traces/{trace_id}: not found (R7.4) -------------------------------


def test_get_trace_absent_returns_404_exclusively(monkeypatch) -> None:
    """A valid trace_id with no stored trace yields HTTP 404 only (R7.4)."""
    store = EmptyTraceStore()
    client = _client(monkeypatch, trace_store=store)

    response = client.get(f"/traces/{VALID_TRACE_ID}")

    assert response.status_code == 404
    # The store was consulted with the requested (valid) id.
    assert store.get_trace_calls == [VALID_TRACE_ID]
    # Not-found path is signalled exclusively by 404, not an empty 200 body.
    body = response.json()
    assert "detail" in body


def test_get_trace_malformed_id_is_400_not_404(monkeypatch) -> None:
    """Format rejection (R7.3) precedes the not-found path and never hits 404."""
    store = EmptyTraceStore()
    client = _client(monkeypatch, trace_store=store)

    response = client.get("/traces/not-a-valid-trace-id")

    assert response.status_code == 400
    # A malformed id is rejected before the store is consulted.
    assert store.get_trace_calls == []


# -- GET /traces: empty search result (R8.10) -------------------------------


def test_search_traces_no_matches_returns_empty_200(monkeypatch) -> None:
    """A valid search matching no traces returns an empty 200 (R8.10)."""
    store = EmptyTraceStore()
    client = _client(monkeypatch, trace_store=store)

    response = client.get("/traces", params={"route": "/never-matches"})

    assert response.status_code == 200
    assert response.json() == []
    # The search ran (the empty result came from the store, not validation).
    assert len(store.search_filters) == 1
    assert store.search_filters[0].route == "/never-matches"


def test_search_traces_no_filters_empty_returns_200(monkeypatch) -> None:
    """An unfiltered search over an empty store still returns an empty 200."""
    store = EmptyTraceStore()
    client = _client(monkeypatch, trace_store=store)

    response = client.get("/traces")

    assert response.status_code == 200
    assert response.json() == []


# -- GET /logs/{trace_id}: empty result (R15.4) -----------------------------


def test_get_logs_by_trace_none_returns_empty_200(monkeypatch) -> None:
    """A valid trace_id with no correlated records returns an empty 200 (R15.4)."""
    store = EmptyLogStore()
    client = _client(monkeypatch, log_store=store)

    response = client.get(f"/logs/{VALID_TRACE_ID}")

    assert response.status_code == 200
    assert response.json() == []
    assert store.get_by_trace_calls == [VALID_TRACE_ID]


# -- GET /logs: empty search result (R16.9) ---------------------------------


def test_search_logs_no_matches_returns_empty_200(monkeypatch) -> None:
    """A valid search matching no log records returns an empty 200 (R16.9)."""
    store = EmptyLogStore()
    client = _client(monkeypatch, log_store=store)

    response = client.get("/logs", params={"level": "CRITICAL"})

    assert response.status_code == 200
    assert response.json() == []
    assert len(store.search_filters) == 1
    assert store.search_filters[0].level == "CRITICAL"


def test_search_logs_no_filters_empty_returns_200(monkeypatch) -> None:
    """An unfiltered search over an empty store still returns an empty 200."""
    store = EmptyLogStore()
    client = _client(monkeypatch, log_store=store)

    response = client.get("/logs")

    assert response.status_code == 200
    assert response.json() == []
