"""Unit test for the SQL Lab executor transaction sequence (task 3.2).

Feature: sql-lab (Slice 1 — read-only viewer connection).

This focused example-based test pins down the *exact ordering* of the
``PostgresCopilotExecutor`` transaction pattern that :class:`SqlLabExecutor`
mirrors verbatim (R4.4). A single shared event log records every meaningful
operation as it happens — connection ``execute`` calls, the cursor ``fetchmany``
call, and the transaction ``rollback`` — so the sequence can be asserted in
order without a live database:

    1. ``SET TRANSACTION READ ONLY``
    2. ``set_config('statement_timeout', <ms>, true)`` — transaction-local, with
       the millisecond value from Settings and the third argument ``true``
    3. ``fetchmany`` on the cursor (fetching the submitted statement's rows)
    4. ``rollback`` — always run, even on the success path

It complements ``test_sql_lab_env_wiring_unit.py`` (which asserts connection
kwargs and timeout wiring) by proving the *relative order* of ``fetchmany`` and
``rollback`` and that rollback is always issued.

The connection is patched via ``psycopg.connect`` (matching the sibling test's
``_SpyConnection`` convention) so no database is required. ``Settings`` is built
in isolation from field aliases.

_Requirements: 4.4_
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from rag_system.config import Settings
from rag_system.sql_lab.executor import SqlLabExecutor

# Required Settings supplied by alias so the model builds in isolation
# (mirrors the convention in the sibling wiring/config tests).
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(**overrides: object) -> Settings:
    """Construct ``Settings`` with the required aliases plus any overrides."""
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


class _SequenceCursor:
    """Cursor whose ``fetchmany`` appends to the shared ordered event log."""

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self._events = events
        self.description = [self._Col("n")]

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        self._events.append(("fetchmany", size))
        return []


class _SequenceConnection:
    """Records execute/fetchmany/rollback into one shared, ordered event log."""

    def __init__(self, events: list[tuple[str, Any]], **kwargs: Any) -> None:
        self._events = events
        self.connect_kwargs = kwargs
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> _SequenceCursor:
        self._events.append(("execute", (sql, params)))
        return _SequenceCursor(self._events)

    def rollback(self) -> None:
        self._events.append(("rollback", None))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def sequence_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Patch ``psycopg.connect`` to feed one shared ordered event log."""
    events: list[tuple[str, Any]] = []

    def _fake_connect(**kwargs: Any) -> _SequenceConnection:
        return _SequenceConnection(events, **kwargs)

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    return events


def test_transaction_sequence_is_ordered_and_rollback_always_runs(
    sequence_events: list[tuple[str, Any]],
) -> None:
    """Assert the exact SET/set_config/fetchmany/rollback order on the success path."""
    config = _build_settings(
        COPILOT_DB_HOST="db.internal",
        COPILOT_DB_NAME="copilot",
        SQL_VIEWER_DB_USER="sql_viewer",
        SQL_VIEWER_DB_PASSWORD="viewer-secret",
        SQL_LAB_STATEMENT_TIMEOUT_MS=9_876,
    )

    SqlLabExecutor(config).execute("SELECT 42")

    # 1) SET TRANSACTION READ ONLY is the first statement issued.
    assert sequence_events[0] == ("execute", ("SET TRANSACTION READ ONLY", None))

    # 2) statement_timeout applied transaction-locally: the ms value from
    #    Settings as the second arg and the literal third argument ``true``.
    kind, (sql, params) = sequence_events[1]
    assert kind == "execute"
    assert "set_config('statement_timeout'" in sql
    assert ", true)" in sql
    assert params == ("9876",)

    # 3) The submitted statement is executed, then rows are fetched via fetchmany.
    assert sequence_events[2] == ("execute", ("SELECT 42", None))
    assert sequence_events[3][0] == "fetchmany"

    # 4) rollback is always run, and it is the final recorded operation.
    assert sequence_events[4] == ("rollback", None)

    # The overall order is exactly: SET → set_config → execute(sql) → fetchmany → rollback.
    kinds = [kind for kind, _ in sequence_events]
    assert kinds == ["execute", "execute", "execute", "fetchmany", "rollback"]

    # rollback appears exactly once and strictly after the fetch.
    assert kinds.count("rollback") == 1
    assert kinds.index("fetchmany") < kinds.index("rollback")
