"""SQL Lab audit store (write-capable copilot-role persistence, Slice 3).

Requirement 8 requires that **every** SQL Lab execution — success, post-guard
error, or guard rejection — leaves an accountable trail of who ran what and
when. :class:`SqlLabAuditStore` is the durable persistence layer for that trail.

Unlike :class:`~rag_system.sql_lab.executor.SqlLabExecutor` and
:class:`~rag_system.sql_lab.schema_lister.SqlLabSchemaLister`, which connect with
the dedicated **read-only** ``SQL_VIEWER_DB_*`` role, the audit store writes as
the **copilot (write-capable)** role over the shared ``COPILOT_DB_*`` connection
— exactly as :class:`rag_system.observability_tracing.log_store.PostgresLogStore`
and :class:`rag_system.observability_tracing.trace_store.PostgresTraceStore` do —
because it must ``INSERT`` into the ``sql_lab_audit`` table, and the read-only
viewer role holds no write privilege anywhere.

This module implements task 11.1: the :class:`SqlLabAuditRecord` data model
(record construction) and :class:`SqlLabAuditStore` (audit table DDL/migration +
:meth:`SqlLabAuditStore.persist`). Wiring the store into
:class:`~rag_system.sql_lab.service.SqlLabService` (one record per outcome, with
the R8.6 "could not record" error path) is task 11.2.

Requirements covered (task 11.1):

* R8.1 — a success record carries the user identity, submitted SQL, a UTC
  timestamp, the execution duration, the returned row count, and a ``success``
  outcome.
* R8.2 — a post-guard execution failure record carries the identity, SQL, UTC
  timestamp, measured duration, and an ``error`` outcome.
* R8.3 — a guard rejection record carries the identity, SQL, a UTC timestamp,
  and a ``rejected`` outcome (duration/row count absent).
* R8.4 — the record identifies the requesting user by the identity supplied in
  the validated JWT.
* R8.5 — the stored SQL is truncated to at most 10000 characters.

Unlike the best-effort observability stores (which never raise), a persistence
failure here surfaces as :class:`SqlLabAuditError` so the service layer can
withhold result rows (R8.6, task 11.2).

Testability
-----------
Mirroring the observability stores, :class:`SqlLabAuditStore` accepts an
injected ``connection_factory`` — any zero-argument callable returning a
psycopg-style connection usable as a context manager (commit + close on a clean
exit, rollback on an exception) — so its persistence behaviour can be exercised
without a live database. When no factory is supplied the store connects using
the ``COPILOT_DB_*`` settings, validating the required values up front.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from rag_system.config import Settings
from rag_system.observability import get_logger
from rag_system.sql_lab.errors import SqlLabAuditError, SqlLabConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextlib import AbstractContextManager

logger = get_logger(__name__)

__all__ = [
    "AUDIT_MAX_SQL_LENGTH",
    "AuditOutcome",
    "SqlLabAuditRecord",
    "SqlLabAuditStore",
    "SQL_LAB_AUDIT_DDL",
    "INSERT_AUDIT_SQL",
]

#: Maximum stored length of the submitted SQL in an audit record (R8.5).
AUDIT_MAX_SQL_LENGTH = 10_000

#: The three mutually exclusive outcomes an audited request can end in.
AuditOutcome = Literal["success", "error", "rejected"]

#: Allowed outcome values, kept in sync with the ``outcome`` CHECK constraint.
_ALLOWED_OUTCOMES: frozenset[str] = frozenset({"success", "error", "rejected"})


# ---------------------------------------------------------------------------
# Audit table DDL (idempotent) + insert statement
# ---------------------------------------------------------------------------

SQL_LAB_AUDIT_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sql_lab_audit (
        id            UUID PRIMARY KEY,
        user_identity TEXT NOT NULL,
        sql           TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL,
        duration_ms   BIGINT CHECK (duration_ms >= 0),
        row_count     BIGINT CHECK (row_count >= 0),
        outcome       TEXT NOT NULL CHECK (outcome IN ('success','error','rejected')),
        error_detail  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS sql_lab_audit_created_at_idx ON sql_lab_audit (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS sql_lab_audit_user_idx       ON sql_lab_audit (user_identity)",
    "CREATE INDEX IF NOT EXISTS sql_lab_audit_outcome_idx    ON sql_lab_audit (outcome)",
)
"""DDL for the ``sql_lab_audit`` table and its ordering/filtering indexes.

Written with ``CREATE TABLE/INDEX IF NOT EXISTS`` so :meth:`SqlLabAuditStore.create_schema`
can run repeatedly (e.g. on every app startup) without error. ``duration_ms`` and
``row_count`` are nullable because a guard rejection records neither (R8.3)."""

INSERT_AUDIT_SQL = """
    INSERT INTO sql_lab_audit
        (id, user_identity, sql, created_at, duration_ms, row_count, outcome, error_detail)
    VALUES (%s, %s, %s, %s::timestamptz, %s, %s, %s, %s)
"""
"""Insert a single ``sql_lab_audit`` row. ``created_at`` is bound as an ISO-8601
UTC string cast to ``timestamptz`` (R8.1); ``duration_ms``/``row_count`` are
bound as ``NULL`` for outcomes that do not carry them (R8.3)."""


# ---------------------------------------------------------------------------
# Audit record (data model + construction)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlLabAuditRecord:
    """One audit-trail entry describing a single SQL Lab request outcome.

    Construct records with :meth:`create`, which assigns the ``id``, truncates
    the SQL to ``AUDIT_MAX_SQL_LENGTH`` (R8.5), and stamps ``created_at`` in UTC
    (R8.1–R8.3). The fields mirror the design's ``SqlLabAuditRecord`` data model:

    * ``id`` — UUID primary key.
    * ``user_identity`` — the identity from the validated JWT (R8.4).
    * ``sql`` — the submitted SQL, truncated to ≤ 10000 characters (R8.5).
    * ``created_at`` — timezone-aware UTC timestamp.
    * ``duration_ms`` — measured execution time; ``None`` for guard rejections.
    * ``row_count`` — returned row count; present on success, else ``None``.
    * ``outcome`` — ``success`` | ``error`` | ``rejected``.
    * ``error_detail`` — failure/rejection detail; ``None`` on success.
    """

    id: uuid.UUID
    user_identity: str
    sql: str
    created_at: datetime
    outcome: AuditOutcome
    duration_ms: int | None = None
    row_count: int | None = None
    error_detail: str | None = None

    @classmethod
    def create(
        cls,
        *,
        user_identity: str,
        sql: str,
        outcome: AuditOutcome,
        duration_ms: int | None = None,
        row_count: int | None = None,
        error_detail: str | None = None,
        created_at: datetime | None = None,
    ) -> SqlLabAuditRecord:
        """Build a record, assigning the id, truncating SQL, and stamping UTC.

        ``sql`` is truncated to the first ``AUDIT_MAX_SQL_LENGTH`` characters
        (R8.5). ``created_at`` defaults to ``datetime.now(timezone.utc)`` and is
        normalised to UTC (R8.1–R8.3). ``outcome`` must be one of ``success``,
        ``error``, or ``rejected``.
        """
        if outcome not in _ALLOWED_OUTCOMES:
            raise ValueError(
                f"invalid audit outcome {outcome!r}: must be one of "
                f"{sorted(_ALLOWED_OUTCOMES)}"
            )
        stamped = created_at or datetime.now(timezone.utc)
        return cls(
            id=uuid.uuid4(),
            user_identity=user_identity,
            sql=sql[:AUDIT_MAX_SQL_LENGTH],
            created_at=_to_utc(stamped),
            outcome=outcome,
            duration_ms=duration_ms,
            row_count=row_count,
            error_detail=error_detail,
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SqlLabAuditStore:
    """Persist SQL Lab audit records as the write-capable copilot role.

    Args:
        settings: Application settings carrying the ``COPILOT_DB_*`` connection
            values, reused by the default connection factory (the copilot role
            holds ``INSERT`` privilege on ``sql_lab_audit``, unlike the read-only
            viewer role).
        connection_factory: Optional zero-argument callable returning a
            psycopg-style connection usable as a context manager (commit + close
            on a clean exit, rollback on an exception). Defaults to connecting
            via the ``COPILOT_DB_*`` settings. Injecting a factory makes the
            persist behaviour testable without a live database.
        logger_: Optional logger; defaults to this module's logger.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        logger_: Any | None = None,
    ) -> None:
        self._settings = settings
        self._connection_factory = connection_factory or self._default_connection_factory
        self._logger = logger_ or logger

    # -- connection ---------------------------------------------------------

    def _default_connection_factory(self) -> AbstractContextManager[Any]:
        """Open a psycopg connection as the copilot role using ``COPILOT_DB_*``.

        Mirrors :class:`rag_system.copilot.PostgresCopilotExecutor` and the
        observability stores: psycopg is imported lazily so the dependency is
        only required when a live database is used, and the required connection
        values are validated up front with a keyed, value-free error naming any
        missing setting (never the secret value).
        """
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError("Install psycopg[binary] to use SQL Lab audit.") from exc

        required = {
            "COPILOT_DB_HOST": self._settings.copilot_db_host,
            "COPILOT_DB_NAME": self._settings.copilot_db_name,
            "COPILOT_DB_USER": self._settings.copilot_db_user,
            "COPILOT_DB_PASSWORD": self._settings.copilot_db_password,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SqlLabConfigError(
                f"Missing SQL Lab audit configuration: {', '.join(missing)}"
            )

        return psycopg.connect(
            host=self._settings.copilot_db_host,
            port=self._settings.copilot_db_port,
            dbname=self._settings.copilot_db_name,
            user=self._settings.copilot_db_user,
            password=self._settings.copilot_db_password,
            sslmode=self._settings.copilot_db_sslmode,
        )

    # -- schema -------------------------------------------------------------

    def create_schema(self) -> None:
        """Apply the ``sql_lab_audit`` DDL idempotently (migration).

        Executes every statement in :data:`SQL_LAB_AUDIT_DDL` and commits once.
        Because each statement uses ``IF NOT EXISTS`` the call is safe to run
        repeatedly (e.g. on app startup).
        """
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                for statement in SQL_LAB_AUDIT_DDL:
                    cur.execute(statement)

    # -- persist ------------------------------------------------------------

    def persist(self, record: SqlLabAuditRecord) -> None:
        """Persist a single audit *record* inside one transaction.

        Every field is written; ``duration_ms``/``row_count`` are stored as SQL
        ``NULL`` when absent (R8.3), and ``created_at`` is bound as an ISO-8601
        UTC string (R8.1). A clean exit from the connection context commits;
        any failure rolls back and is re-raised as :class:`SqlLabAuditError` so
        the caller (the service) can withhold result rows (R8.6), never leaving
        a partial record behind.
        """
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        INSERT_AUDIT_SQL,
                        (
                            str(record.id),
                            record.user_identity,
                            record.sql,
                            _to_iso_utc(record.created_at),
                            record.duration_ms,
                            record.row_count,
                            record.outcome,
                            record.error_detail,
                        ),
                    )
        except SqlLabAuditError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a mandatory-audit failure
            raise SqlLabAuditError(
                "Failed to persist the SQL Lab audit record."
            ) from exc


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _to_utc(value: datetime) -> datetime:
    """Normalise a ``datetime`` to a timezone-aware UTC ``datetime``.

    A naive ``datetime`` is assumed to already be UTC; an aware ``datetime`` is
    converted to UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_iso_utc(value: datetime) -> str:
    """Render a ``datetime`` as an ISO-8601 string normalised to UTC."""
    return _to_utc(value).isoformat()
