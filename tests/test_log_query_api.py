"""Unit tests for the Log_Query_Service HTTP endpoints (task 14.2).

Exercises ``GET /logs/{trace_id}`` and ``GET /logs`` registered on the existing
FastAPI app, covering trace_id format validation (R15.3), empty-200 results
(R15.4, R16.9), inclusive ordering pass-through (R15.2, R16.5), and request
validation for an inverted time range (R16.7) and out-of-range limit (R16.8).

The endpoints are exercised against an in-process fake log store injected via
``get_log_store`` so no live PostgreSQL is required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.observability_tracing.log_store import LogSearchFilters
from rag_system.observability_tracing.models import LogRecordModel

VALID_TRACE_ID = "0123456789abcdef0123456789abcdef"


class FakeLogStore:
    """Captures query arguments and returns canned records."""

    def __init__(self, records: list[LogRecordModel] | None = None) -> None:
        self._records = records or []
        self.get_by_trace_calls: list[str] = []
        self.search_filters: list[LogSearchFilters] = []

    def get_by_trace(self, trace_id: str) -> list[LogRecordModel]:
        self.get_by_trace_calls.append(trace_id)
        return [r for r in self._records if r.trace_id == trace_id]

    def search(self, filters: LogSearchFilters) -> list[LogRecordModel]:
        self.search_filters.append(filters)
        return list(self._records)


def _record(trace_id: str | None = VALID_TRACE_ID) -> LogRecordModel:
    return LogRecordModel(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        level="INFO",
        logger="rag_system.test",
        message="hello",
        trace_id=trace_id,
        exc_text=None,
        extra={"k": "v"},
        insertion_seq=1,
    )


def _client(monkeypatch, store: FakeLogStore) -> TestClient:
    monkeypatch.setattr(api_module, "get_log_store", lambda: store)
    return TestClient(api_module.app)


# -- GET /logs/{trace_id} ---------------------------------------------------


def test_get_logs_by_trace_returns_matching_records(monkeypatch) -> None:
    store = FakeLogStore([_record()])
    client = _client(monkeypatch, store)

    response = client.get(f"/logs/{VALID_TRACE_ID}")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["trace_id"] == VALID_TRACE_ID
    assert body[0]["message"] == "hello"
    assert store.get_by_trace_calls == [VALID_TRACE_ID]


def test_get_logs_by_trace_empty_returns_200(monkeypatch) -> None:
    """A valid trace_id with no records yields an empty 200 (R15.4)."""
    store = FakeLogStore([])
    client = _client(monkeypatch, store)

    response = client.get(f"/logs/{VALID_TRACE_ID}")

    assert response.status_code == 200
    assert response.json() == []


def test_get_logs_by_trace_rejects_malformed_id(monkeypatch) -> None:
    """A non-conforming trace_id is rejected with 400 (R15.3)."""
    store = FakeLogStore([_record()])
    client = _client(monkeypatch, store)

    for bad in ("not-hex", "ABCDEF0123456789abcdef0123456789", "abc", "0" * 33):
        response = client.get(f"/logs/{bad}")
        assert response.status_code == 400, bad
        assert "trace_id" in response.json()["detail"]
    # Never queried the store on a rejected id.
    assert store.get_by_trace_calls == []


# -- GET /logs --------------------------------------------------------------


def test_search_logs_applies_filters_and_default_limit(monkeypatch) -> None:
    store = FakeLogStore([_record()])
    client = _client(monkeypatch, store)

    response = client.get(
        "/logs",
        params={
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-02T00:00:00+00:00",
            "level": "INFO",
            "trace_id": VALID_TRACE_ID,
        },
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert len(store.search_filters) == 1
    f = store.search_filters[0]
    assert f.level == "INFO"
    assert f.trace_id == VALID_TRACE_ID
    assert f.limit == 100  # default when omitted (R16.5)


def test_search_logs_empty_returns_200(monkeypatch) -> None:
    """A valid search matching nothing returns empty 200 (R16.9)."""
    store = FakeLogStore([])
    client = _client(monkeypatch, store)

    response = client.get("/logs", params={"level": "DEBUG"})

    assert response.status_code == 200
    assert response.json() == []


def test_search_logs_rejects_inverted_range(monkeypatch) -> None:
    """end earlier than start is rejected with 400 (R16.7)."""
    store = FakeLogStore([_record()])
    client = _client(monkeypatch, store)

    response = client.get(
        "/logs",
        params={
            "start": "2024-01-02T00:00:00+00:00",
            "end": "2024-01-01T00:00:00+00:00",
        },
    )

    assert response.status_code == 400
    assert "range" in response.json()["detail"].lower()
    assert store.search_filters == []


def test_search_logs_rejects_out_of_range_limit(monkeypatch) -> None:
    """A limit outside 1..1000 is rejected with 400 (R16.8)."""
    store = FakeLogStore([_record()])
    client = _client(monkeypatch, store)

    for bad_limit in (0, 1001, -5):
        response = client.get("/logs", params={"limit": bad_limit})
        assert response.status_code == 400, bad_limit
        assert "limit" in response.json()["detail"].lower()
    assert store.search_filters == []


def test_search_logs_accepts_boundary_limits(monkeypatch) -> None:
    store = FakeLogStore([])
    client = _client(monkeypatch, store)

    for ok_limit in (1, 1000):
        response = client.get("/logs", params={"limit": ok_limit})
        assert response.status_code == 200, ok_limit
    assert [f.limit for f in store.search_filters] == [1, 1000]
