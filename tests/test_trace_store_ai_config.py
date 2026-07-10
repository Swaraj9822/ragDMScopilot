"""Round-trip tests for AI-configuration attribution through the trace store.

Feature: ai-observability-platform (R9.1, R9.11).

These tests close the gap that let ``ai_configuration_version_id`` /
``resolved_settings`` be silently dropped by the persistence layer: the fields
were threaded correctly through the models, serializer, and flush workers, but
:data:`INSERT_TRACE_SQL` bound only five columns and ``_TRACE_COLUMNS`` read
back only five, so every trace fetched from the database came back with
``ai_configuration_version_id=None`` regardless of what produced it.

Rather than the abstract in-memory double, this drives the *production*
:class:`PostgresTraceStore` through a fake psycopg-style connection that both
stages INSERTs and answers the read SELECTs, so a regression in the INSERT
binding, the column list, or ``_row_to_trace`` fails here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

from rag_system.config import Settings
from rag_system.observability_tracing.models import Span, Trace
from rag_system.observability_tracing.trace_store import (
    PostgresTraceStore,
    TraceSearchFilters,
)


# ---------------------------------------------------------------------------
# Fake psycopg-style connection: stages INSERTs and answers the read SELECTs.
# ---------------------------------------------------------------------------


class _Backend:
    """Shared committed state across connections (mirrors a real database)."""

    def __init__(self) -> None:
        #: trace_id -> the trace INSERT params (in INSERT/``_TRACE_COLUMNS`` order).
        self.traces: dict[str, tuple[Any, ...]] = {}
        #: trace_id -> list of span INSERT params (in INSERT param order).
        self.spans: dict[str, list[tuple[Any, ...]]] = {}


class _FakeCursor:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        backend = self._conn.backend
        if "INSERT INTO traces" in sql:
            self._conn.staged_trace = (params[0], params)
        elif "INSERT INTO spans" in sql:
            self._conn.staged_spans.append(params)
        elif "FROM traces" in sql and "trace_id = %s" in sql:
            # SELECT_TRACE_SQL — single trace by id (params[0]).
            row = backend.traces.get(params[0])
            self._result = [row] if row is not None else []
        elif "FROM traces" in sql:
            # search_traces — no filters exercised here; return all, newest first,
            # honouring the trailing LIMIT param.
            limit = params[-1] if params else len(backend.traces)
            rows = sorted(backend.traces.values(), key=lambda r: r[2], reverse=True)
            self._result = rows[:limit]
        elif "FROM spans" in sql and "trace_id = ANY" in sql:
            # SELECT_SPANS_FOR_TRACES_SQL — spans for a set, trace_id appended.
            wanted = set(params[0])
            self._result = [
                tuple(sp[1:]) + (sp[0],)
                for tid in wanted
                for sp in backend.spans.get(tid, [])
            ]
        elif "FROM spans" in sql:
            # SELECT_SPANS_SQL — spans for one trace (params[0]); strip trace_id.
            self._result = [tuple(sp[1:]) for sp in backend.spans.get(params[0], [])]
        else:  # pragma: no cover - unexpected query
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)


class _FakeConnection:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.staged_trace: tuple[str, tuple[Any, ...]] | None = None
        self.staged_spans: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, *_: object) -> bool:
        if exc_type is None and self.staged_trace is not None:
            trace_id, params = self.staged_trace
            self.backend.traces[trace_id] = params
            self.backend.spans.setdefault(trace_id, [])
            for span_params in self.staged_spans:
                self.backend.spans[span_params[0]].append(span_params)
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def _store(backend: _Backend) -> PostgresTraceStore:
    settings = cast(Settings, SimpleNamespace())
    return PostgresTraceStore(
        settings, connection_factory=lambda: _FakeConnection(backend)
    )


def _trace(trace_id: str, **overrides: Any) -> Trace:
    base = dict(
        trace_id=trace_id,
        route="/ask",
        start_ts=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        duration_ms=42,
        root_status="success",
        spans=[
            Span(
                span_id="root",
                parent_span_id=None,
                operation="http",
                start_ts=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
                duration_ms=42,
                status="success",
                attributes={},
            )
        ],
    )
    base.update(overrides)
    return Trace(**base)  # type: ignore[arg-type]


def test_get_trace_round_trips_ai_configuration_attribution() -> None:
    backend = _Backend()
    store = _store(backend)
    store.persist(
        _trace(
            "a" * 32,
            ai_configuration_version_id="cfg-v7",
            resolved_settings={"model": "gemini-3.5-flash", "router_threshold": 0.5},
        )
    )

    fetched = store.get_trace("a" * 32)

    assert fetched is not None
    assert fetched.ai_configuration_version_id == "cfg-v7"
    assert fetched.resolved_settings == {
        "model": "gemini-3.5-flash",
        "router_threshold": 0.5,
    }


def test_search_traces_round_trips_ai_configuration_attribution() -> None:
    backend = _Backend()
    store = _store(backend)
    store.persist(
        _trace(
            "b" * 32,
            ai_configuration_version_id="cfg-v9",
            resolved_settings={"model": "gemini-3.1-pro"},
        )
    )

    results = store.search_traces(TraceSearchFilters(limit=10))

    assert len(results) == 1
    assert results[0].ai_configuration_version_id == "cfg-v9"
    assert results[0].resolved_settings == {"model": "gemini-3.1-pro"}


def test_unresolved_configuration_persists_as_null_and_empty_settings() -> None:
    # A trace whose configuration was unresolved (the serializer omits the
    # optional keys) must round-trip as None + {} rather than raising (R9.2).
    backend = _Backend()
    store = _store(backend)
    store.persist(_trace("c" * 32))

    fetched = store.get_trace("c" * 32)

    assert fetched is not None
    assert fetched.ai_configuration_version_id is None
    assert fetched.resolved_settings == {}
