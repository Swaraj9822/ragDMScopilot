"""Unit tests for SQL Lab executor timeout and generic-error paths (task 3.3).

Feature: sql-lab (Slice 1 — read-only viewer executor).

These example-based unit tests complement the connection-assembly wiring tests
in ``test_sql_lab_env_wiring_unit.py``. They pin down the two post-connection
failure paths of :meth:`SqlLabExecutor.execute`:

1. **Timeout (R4.9).** When the running statement trips
   ``statement_timeout`` the driver raises ``psycopg.errors.QueryCanceled``.
   The executor MUST roll the transaction back and raise
   :class:`SqlLabTimeoutError`.

2. **Generic database error (R4.13).** Any other ``psycopg.Error`` MUST cause a
   rollback and be re-raised as :class:`SqlLabExecutionError` carrying the
   underlying database message.

Both paths are exercised with a spy connection (mirroring the sibling test's
``psycopg.connect`` monkeypatch pattern) whose ``execute`` raises the *real*
``psycopg`` exception when the submitted statement runs, so the executor's
``except`` clauses are matched exactly as they would be against a live driver.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from rag_system.config import Settings
from rag_system.sql_lab.errors import SqlLabExecutionError, SqlLabTimeoutError
from rag_system.sql_lab.executor import SqlLabExecutor

# Required Settings supplied by alias so the model builds in isolation
# (mirrors the convention in the sibling env-wiring test).
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}

# The guard-approved statement under test. The spy raises when it runs; the
# transaction-setup statements ("SET TRANSACTION READ ONLY" and the
# ``set_config`` call) always succeed.
_TARGET_SQL = "SELECT id FROM widgets"


def _build_settings(**overrides: object) -> Settings:
    """Construct ``Settings`` with the required aliases plus any overrides."""
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


class _FailingCursor:
    """Cursor stub whose ``fetchmany`` is never expected to be reached."""

    description: list[Any] = []

    def fetchmany(self, size: int) -> list[dict[str, Any]]:  # noqa: ARG002
        raise AssertionError("fetchmany must not run after execute raises")


class _RaisingConnection:
    """Spy connection that raises a configured error when the target SQL runs.

    The transaction-setup statements succeed so the executor reaches the
    ``try: conn.execute(sql)`` block, where the injected ``psycopg`` exception is
    raised — driving the timeout / generic-error branches.
    """

    def __init__(self, error: BaseException, **kwargs: Any) -> None:
        self._error = error
        self.connect_kwargs = kwargs
        self.executed: list[tuple[str, Any]] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> _FailingCursor:
        self.executed.append((sql, params))
        if sql == _TARGET_SQL:
            raise self._error
        return _FailingCursor()

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> dict[str, Any]:
    """Patch ``psycopg.connect`` to return a spy that raises ``error`` on the query."""
    captured: dict[str, Any] = {}

    def _fake_connect(**kwargs: Any) -> _RaisingConnection:
        conn = _RaisingConnection(error, **kwargs)
        captured["conn"] = conn
        return conn

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    return captured


def _viewer_settings() -> Settings:
    """Minimal settings with viewer credentials so ``execute`` reaches the db."""
    return _build_settings(
        COPILOT_DB_HOST="db.internal",
        COPILOT_DB_NAME="copilot",
        SQL_VIEWER_DB_USER="sql_viewer",
        SQL_VIEWER_DB_PASSWORD="viewer-secret",
        SQL_LAB_STATEMENT_TIMEOUT_MS=10_000,
    )


# ---------------------------------------------------------------------------
# R4.9 — statement timeout rolls back and raises SqlLabTimeoutError.
# ---------------------------------------------------------------------------


def test_timeout_rolls_back_and_raises_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A QueryCanceled from the driver becomes a SqlLabTimeoutError after rollback."""
    canceled = psycopg.errors.QueryCanceled(
        "canceling statement due to statement timeout"
    )
    captured = _patch_connect(monkeypatch, canceled)

    with pytest.raises(SqlLabTimeoutError):
        SqlLabExecutor(_viewer_settings()).execute(_TARGET_SQL)

    conn: _RaisingConnection = captured["conn"]
    # Rollback ran (R4.9) and the connection was closed in the finally block.
    assert conn.rolled_back is True
    assert conn.closed is True


# ---------------------------------------------------------------------------
# R4.13 — any other db error rolls back and raises SqlLabExecutionError
#          carrying the underlying database message.
# ---------------------------------------------------------------------------


def test_generic_db_error_rolls_back_and_raises_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-timeout psycopg.Error becomes a SqlLabExecutionError after rollback."""
    db_message = 'relation "widgets" does not exist'
    db_error = psycopg.Error(db_message)
    captured = _patch_connect(monkeypatch, db_error)

    with pytest.raises(SqlLabExecutionError) as excinfo:
        SqlLabExecutor(_viewer_settings()).execute(_TARGET_SQL)

    # The SqlLabExecutionError carries the underlying database message (R4.13).
    assert db_message in str(excinfo.value)

    conn: _RaisingConnection = captured["conn"]
    # Rollback ran (R4.13) and the connection was closed in the finally block.
    assert conn.rolled_back is True
    assert conn.closed is True
