"""Unit tests for SQL Lab env-var wiring and viewer connection assembly (task 1.4).

Feature: sql-lab (Slice 1 — viewer role + configuration).

These example-based unit tests complement the property tests for config bounds
(``test_config_sql_lab_bounds_properties.py``) and missing credentials
(``test_sql_lab_viewer_credentials_properties.py``). They pin down two concrete
wiring guarantees:

1. **Env-var aliases (R1.1, R1.2).** Each SQL Lab ``Settings`` field is read
   from its documented environment alias — ``SQL_VIEWER_DB_USER``,
   ``SQL_VIEWER_DB_PASSWORD``, ``SQL_LAB_ROW_LIMIT``,
   ``SQL_LAB_STATEMENT_TIMEOUT_MS``, ``SQL_LAB_SENSITIVE_TABLES``,
   ``SQL_LAB_ANALYSIS_MODEL_ID``, and ``SQL_LAB_DEEP_ANALYSIS_MODEL_ID`` — and
   defaults apply when the alias is absent.

2. **Connection assembly (R1.3).** ``SqlLabExecutor`` opens its connection with
   the shared ``COPILOT_DB_HOST/PORT/NAME/SSLMODE`` endpoint values while
   substituting the dedicated ``SQL_VIEWER_DB_USER``/``SQL_VIEWER_DB_PASSWORD``
   credentials. A spy on ``psycopg.connect`` captures the connection kwargs so
   the assembly can be asserted without a live database.

The ``Settings`` model uses field *aliases*, so all values are supplied by alias
exactly as they would arrive from the environment. The other required settings
are supplied with throwaway values so the model can be constructed in isolation
without depending on a ``.env`` file (mirrors the convention in the sibling
config tests).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from rag_system.config import Settings
from rag_system.sql_lab.executor import SqlLabExecutor

# Required Settings supplied by alias so the model builds in isolation.
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(**overrides: object) -> Settings:
    """Construct ``Settings`` with the required aliases plus any overrides."""
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R1.1, R1.2 (+1.7, 1.8) — SQL Lab fields read from their env aliases.
# ---------------------------------------------------------------------------


def test_sql_lab_fields_read_from_env_aliases() -> None:
    """Every SQL Lab field reads the value supplied under its documented alias."""
    config = _build_settings(
        SQL_VIEWER_DB_USER="sql_viewer",
        SQL_VIEWER_DB_PASSWORD="viewer-secret",
        SQL_LAB_ROW_LIMIT=250,
        SQL_LAB_STATEMENT_TIMEOUT_MS=15_000,
        SQL_LAB_SENSITIVE_TABLES="users, refresh_tokens, secrets",
        SQL_LAB_ANALYSIS_MODEL_ID="flash-override",
        SQL_LAB_DEEP_ANALYSIS_MODEL_ID="pro-override",
    )

    assert config.sql_viewer_db_user == "sql_viewer"
    assert config.sql_viewer_db_password == "viewer-secret"
    assert config.sql_lab_row_limit == 250
    assert config.sql_lab_statement_timeout_ms == 15_000
    assert config.sql_lab_sensitive_tables == "users, refresh_tokens, secrets"
    assert config.sql_lab_analysis_model_id == "flash-override"
    assert config.sql_lab_deep_analysis_model_id == "pro-override"


def test_sql_lab_fields_fall_back_to_documented_defaults() -> None:
    """Absent aliases fall back to the defaults specified by the design."""
    config = _build_settings()

    assert config.sql_viewer_db_user is None
    assert config.sql_viewer_db_password is None
    assert config.sql_lab_row_limit == 100
    assert config.sql_lab_statement_timeout_ms == 10_000
    assert config.sql_lab_sensitive_tables == "users,refresh_tokens"
    assert config.sql_lab_analysis_model_id == "gemini-3.5-flash"
    assert config.sql_lab_deep_analysis_model_id == "gemini-3.1-pro"


def test_sensitive_tables_alias_feeds_normalized_denylist_set() -> None:
    """The denylist set derives from the aliased comma-separated value."""
    config = _build_settings(SQL_LAB_SENSITIVE_TABLES=" Users , Refresh_Tokens , , api_keys ")

    # Stripped, lowercased, and empty entries dropped.
    assert config.sql_lab_sensitive_tables_set == frozenset(
        {"users", "refresh_tokens", "api_keys"}
    )


# ---------------------------------------------------------------------------
# R1.3 — viewer connection reuses the COPILOT_DB_* endpoint + viewer creds.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal cursor: a zero-row result with a stable column description."""

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

    def __init__(self) -> None:
        self.description = [self._Col("id"), self._Col("label")]

    def fetchmany(self, size: int) -> list[dict[str, Any]]:  # noqa: ARG002
        return []


class _SpyConnection:
    """Records every ``execute`` call and the connect kwargs that built it."""

    def __init__(self, **kwargs: Any) -> None:
        self.connect_kwargs = kwargs
        self.executed: list[tuple[str, Any]] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.executed.append((sql, params))
        return _FakeCursor()

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def spy_connect(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``psycopg.connect`` to capture kwargs and return a spy connection."""
    captured: dict[str, Any] = {}

    def _fake_connect(**kwargs: Any) -> _SpyConnection:
        conn = _SpyConnection(**kwargs)
        captured["conn"] = conn
        captured["kwargs"] = kwargs
        return conn

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    return captured


def test_viewer_connection_reuses_copilot_endpoint_with_viewer_credentials(
    spy_connect: dict[str, Any],
) -> None:
    """The connection endpoint comes from COPILOT_DB_*, creds from SQL_VIEWER_DB_*."""
    config = _build_settings(
        COPILOT_DB_HOST="copilot-db.internal",
        COPILOT_DB_PORT=6543,
        COPILOT_DB_NAME="copilot",
        COPILOT_DB_SSLMODE="verify-full",
        # The copilot role must NOT be used by SQL Lab.
        COPILOT_DB_USER="copilot_role",
        COPILOT_DB_PASSWORD="copilot-secret",
        SQL_VIEWER_DB_USER="sql_viewer",
        SQL_VIEWER_DB_PASSWORD="viewer-secret",
    )

    SqlLabExecutor(config).execute("SELECT 1")

    kwargs = spy_connect["kwargs"]
    # Endpoint reused verbatim from the COPILOT_DB_* settings.
    assert kwargs["host"] == "copilot-db.internal"
    assert kwargs["port"] == 6543
    assert kwargs["dbname"] == "copilot"
    assert kwargs["sslmode"] == "verify-full"
    # Credentials are the dedicated viewer role, never the copilot role.
    assert kwargs["user"] == "sql_viewer"
    assert kwargs["password"] == "viewer-secret"
    assert kwargs["password"] != "copilot-secret"
    assert kwargs["user"] != "copilot_role"


def test_viewer_connection_applies_row_limit_and_timeout_from_settings(
    spy_connect: dict[str, Any],
) -> None:
    """The executor applies the aliased timeout and always rolls back + closes."""
    config = _build_settings(
        COPILOT_DB_HOST="db.internal",
        COPILOT_DB_NAME="copilot",
        SQL_VIEWER_DB_USER="sql_viewer",
        SQL_VIEWER_DB_PASSWORD="viewer-secret",
        SQL_LAB_STATEMENT_TIMEOUT_MS=12_345,
    )

    result = SqlLabExecutor(config).execute("SELECT 1")

    conn: _SpyConnection = spy_connect["conn"]
    statements = [sql for sql, _ in conn.executed]
    # Transaction pattern reused from the copilot executor.
    assert statements[0] == "SET TRANSACTION READ ONLY"
    # Timeout applied transaction-locally with the value from Settings.
    set_config_call = conn.executed[1]
    assert "set_config('statement_timeout'" in set_config_call[0]
    assert set_config_call[1] == ("12345",)
    # The submitted statement executed, then rolled back and closed.
    assert statements[2] == "SELECT 1"
    assert conn.rolled_back is True
    assert conn.closed is True
    # Columns come from the cursor description; zero rows, not truncated.
    assert result.columns == ["id", "label"]
    assert result.rows == []
    assert result.truncated is False
