"""Tests for the auth schema helpers that don't require a live database.

``connect`` / ``apply_schema`` against a real PostgreSQL are covered by the
integration suite; here we cover the missing-configuration guard and the
idempotent DDL execution via a fake connection.
"""

from __future__ import annotations

import pytest

from rag_system.auth import schema

from auth_doubles import make_settings


class _FakeCursor:
    def __init__(self, executed: list[str]):
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._executed.append(sql)


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []
        self.committed = False

    def cursor(self):
        return _FakeCursor(self.executed)

    def commit(self):
        self.committed = True


def test_connect_raises_when_db_settings_missing():
    # make_settings leaves COPILOT_DB_* unset, so connect must refuse clearly.
    settings = make_settings()
    with pytest.raises(RuntimeError) as exc:
        schema.connect(settings)
    message = str(exc.value)
    assert "COPILOT_DB_HOST" in message


def test_create_schema_runs_all_ddl_and_commits():
    conn = _FakeConn()
    schema.create_schema(conn)
    assert conn.committed is True
    # Every DDL statement was executed, and both tables are created.
    assert len(conn.executed) == len(schema.SCHEMA_DDL)
    joined = "\n".join(conn.executed)
    assert "CREATE TABLE IF NOT EXISTS users" in joined
    assert "CREATE TABLE IF NOT EXISTS refresh_tokens" in joined
