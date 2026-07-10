"""PostgreSQL schema (DDL) for the AI observability tracing platform.

This module defines the ``traces``, ``spans``, and ``log_records`` tables plus
the ordering/filtering indexes exactly as specified in the design's PostgreSQL
schema section, and provides helpers to apply the DDL idempotently.

The DDL is written with ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT
EXISTS`` so :func:`create_schema` can be invoked repeatedly (e.g. on every app
startup) without error.

Design references:
- ``traces``    : R5.2 (UTC ``start_ts``), R13.2 (cascade target).
- ``spans``     : R5.3 (null parent for root), R7.2 (ordering index),
                  R13.2 (``ON DELETE CASCADE`` from ``traces``).
- ``log_records``: R14.2 (UTC ``ts``), R14.3 (explicit-null ``trace_id``),
                  R15.2 (insertion-order identity + trace ordering index).

Database connectivity reuses the existing ``COPILOT_DB_*`` settings exposed by
:class:`rag_system.config.Settings`, matching the psycopg connection style used
by :class:`rag_system.copilot.PostgresCopilotExecutor` and ``rdscon.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rag_system.config import Settings
from rag_system.observability import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

logger = get_logger(__name__)

__all__ = [
    "TRACES_DDL",
    "SPANS_DDL",
    "LOG_RECORDS_DDL",
    "SCHEMA_DDL",
    "connect",
    "create_schema",
    "apply_schema",
]


# ---------------------------------------------------------------------------
# DDL statements (idempotent)
# ---------------------------------------------------------------------------

TRACES_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS traces (
        trace_id      TEXT PRIMARY KEY,
        route         TEXT NOT NULL,
        start_ts      TIMESTAMPTZ NOT NULL,
        duration_ms   BIGINT NOT NULL CHECK (duration_ms >= 0),
        root_status   TEXT NOT NULL CHECK (root_status IN ('success','error')),
        ai_configuration_version_id TEXT,
        resolved_settings           JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    # Migrations for ``traces`` tables created before the AI-configuration
    # attribution columns existed (R9.1, R9.11). ``ADD COLUMN IF NOT EXISTS`` is
    # idempotent, so these run safely on every startup alongside the CREATE
    # TABLE above and bring an older table up to the current shape without a
    # separate manual migration step.
    "ALTER TABLE traces ADD COLUMN IF NOT EXISTS ai_configuration_version_id TEXT",
    "ALTER TABLE traces ADD COLUMN IF NOT EXISTS resolved_settings JSONB NOT NULL DEFAULT '{}'::jsonb",
    "CREATE INDEX IF NOT EXISTS traces_start_ts_idx ON traces (start_ts DESC)",
    "CREATE INDEX IF NOT EXISTS traces_route_idx    ON traces (route)",
    "CREATE INDEX IF NOT EXISTS traces_status_idx   ON traces (root_status)",
)
"""DDL for the ``traces`` table and its ordering/filtering indexes.

Includes the ``ai_configuration_version_id`` / ``resolved_settings`` columns
(R9.1, R9.11) both in the ``CREATE TABLE`` (new deployments) and as idempotent
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` migrations (existing deployments)."""

SPANS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS spans (
        trace_id       TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
        span_id        TEXT NOT NULL,
        parent_span_id TEXT,
        operation      TEXT NOT NULL,
        start_ts       TIMESTAMPTZ NOT NULL,
        duration_ms    BIGINT NOT NULL CHECK (duration_ms >= 0),
        status         TEXT NOT NULL CHECK (status IN ('success','error')),
        attributes     JSONB NOT NULL DEFAULT '{}'::jsonb,
        PRIMARY KEY (trace_id, span_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS spans_trace_order_idx ON spans (trace_id, start_ts, span_id)",
)
"""DDL for the ``spans`` table (FK cascade to ``traces``) and its ordering index."""

LOG_RECORDS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS log_records (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ts            TIMESTAMPTZ NOT NULL,
        level         TEXT NOT NULL,
        logger        TEXT NOT NULL,
        message       TEXT NOT NULL,
        trace_id      TEXT,
        exc_text      TEXT,
        extra         JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS log_records_trace_idx ON log_records (trace_id, ts DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS log_records_ts_idx    ON log_records (ts DESC)",
    "CREATE INDEX IF NOT EXISTS log_records_level_idx ON log_records (level)",
)
"""DDL for the ``log_records`` table and its trace/ts/level indexes."""

# Ordered so that the FK target (``traces``) is created before ``spans``.
SCHEMA_DDL: tuple[str, ...] = (*TRACES_DDL, *SPANS_DDL, *LOG_RECORDS_DDL)
"""All schema DDL statements in dependency order."""


# ---------------------------------------------------------------------------
# Connection + apply helpers
# ---------------------------------------------------------------------------


def connect(settings: Settings) -> Connection[Any]:
    """Open a psycopg connection using the ``COPILOT_DB_*`` settings.

    Mirrors :class:`rag_system.copilot.PostgresCopilotExecutor`: psycopg is
    imported lazily so the dependency is only required when a live database is
    used, and the required connection settings are validated up front with a
    clear error naming any missing values.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Install psycopg[binary] to apply the observability schema."
        ) from exc

    required = {
        "COPILOT_DB_HOST": settings.copilot_db_host,
        "COPILOT_DB_NAME": settings.copilot_db_name,
        "COPILOT_DB_USER": settings.copilot_db_user,
        "COPILOT_DB_PASSWORD": settings.copilot_db_password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing observability database setting(s): {', '.join(missing)}"
        )

    return psycopg.connect(
        host=settings.copilot_db_host,
        port=settings.copilot_db_port,
        dbname=settings.copilot_db_name,
        user=settings.copilot_db_user,
        password=settings.copilot_db_password,
        sslmode=settings.copilot_db_sslmode,
    )


def create_schema(conn: Connection[Any]) -> None:
    """Apply the full observability schema idempotently on an open connection.

    Executes every statement in :data:`SCHEMA_DDL` (tables in dependency order
    followed by indexes) and commits once. Because each statement uses
    ``IF NOT EXISTS`` the call is safe to run repeatedly. The caller owns the
    connection lifecycle.
    """
    with conn.cursor() as cur:
        for statement in SCHEMA_DDL:
            cur.execute(statement)
    conn.commit()
    logger.info("Applied observability schema", extra={"statements": len(SCHEMA_DDL)})


def apply_schema(settings: Settings) -> None:
    """Connect using ``COPILOT_DB_*`` settings and apply the schema idempotently.

    Convenience wrapper around :func:`connect` + :func:`create_schema` that
    manages the connection lifecycle.
    """
    with connect(settings) as conn:
        create_schema(conn)
