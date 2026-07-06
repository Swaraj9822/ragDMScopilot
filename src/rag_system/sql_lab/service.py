"""SQL Lab orchestration service (guard → execute → shape Result_Set).

:class:`SqlLabService` is the single entry point the ``POST /sql/run`` route
calls. It ties the two Slice 1 security/execution pieces together in the exact
order the design mandates:

1. **Guard first (secondary guardrail).** The submitted statement is handed to
   :class:`~rag_system.sql_lab.guard.SqlLabGuard` *before* anything touches the
   database. If the guard rejects it (:class:`SqlLabValidationError`), the
   executor is never invoked, so a rejected statement can never reach the
   database and database state is left unchanged (R3.10, R4.11, R4.12).
2. **Execute (viewer role).** Only a guard-approved statement is run by
   :class:`~rag_system.sql_lab.executor.SqlLabExecutor`, which fetches
   ``Row_Limit + 1`` rows so truncation can be detected without a second
   round-trip.
3. **Shape the Result_Set.** The service trims the fetched rows down to
   ``Row_Limit``, sets ``truncated`` accordingly, reports ``row_count`` as the
   number of returned rows, echoes the submitted ``sql`` verbatim, and reports
   ``duration_ms`` as the measured whole-millisecond execution time (R4.5–R4.8,
   R4.10).

Slice 3 audit wiring (task 11.2) layers a mandatory audit trail over the three
possible outcomes: the service persists **exactly one**
:class:`~rag_system.sql_lab.audit_store.SqlLabAuditRecord` per request — a
``rejected`` record when the guard rejects (R8.3), an ``error`` record when a
guard-approved statement fails to execute (R8.2), or a ``success`` record when
it succeeds (R8.1). Because auditing is mandatory, the success record is
persisted *before* the rows are returned: if persistence fails
(:class:`~rag_system.sql_lab.errors.SqlLabAuditError`) the error propagates and
the caller never receives the Result_Set (R8.6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rag_system.config import Settings
from rag_system.sql_lab.audit_store import (
    AuditOutcome,
    SqlLabAuditRecord,
    SqlLabAuditStore,
)
from rag_system.sql_lab.errors import SqlLabError
from rag_system.sql_lab.executor import SqlLabExecutor
from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError
from rag_system.sql_lab.schema_lister import SchemaTable, SqlLabSchemaLister


@dataclass(frozen=True)
class SqlRunResult:
    """The shaped Result_Set returned by :meth:`SqlLabService.run`.

    Field names mirror the design's ``Result_Set`` contract. The route layer
    serializes these to the camelCase JSON shape
    (``rowCount``/``durationMs``) consumed by the frontend.

    * ``columns`` — column names in select order.
    * ``rows`` — at most ``Row_Limit`` rows.
    * ``row_count`` — ``len(rows)`` actually returned (``rowCount``).
    * ``duration_ms`` — whole-millisecond measured execution time (``durationMs``).
    * ``sql`` — the submitted SQL, echoed back verbatim.
    * ``truncated`` — ``True`` iff the query produced more than ``Row_Limit`` rows.
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    duration_ms: int
    sql: str
    truncated: bool


class SqlLabService:
    """Orchestrate guard → execute and shape the Result_Set for ``POST /sql/run``.

    The guard is constructed from the ``Settings`` sensitive-table denylist so a
    denylisted table is rejected before execution (R2.4). ``guard``,
    ``executor``, ``schema_lister``, and ``audit_store`` may be injected
    (primarily for testing); by default they are built from ``settings``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        guard: SqlLabGuard | None = None,
        executor: SqlLabExecutor | None = None,
        schema_lister: SqlLabSchemaLister | None = None,
        audit_store: SqlLabAuditStore | None = None,
    ) -> None:
        self._settings = settings
        self._guard = guard or SqlLabGuard(settings.sql_lab_sensitive_tables_set)
        self._executor = executor or SqlLabExecutor(settings)
        self._schema_lister = schema_lister or SqlLabSchemaLister(settings)
        self._audit_store = audit_store or SqlLabAuditStore(settings)

    def run(self, sql: str, user_identity: str) -> SqlRunResult:
        """Validate, execute, shape the Result_Set, and audit the outcome.

        Exactly one audit record is persisted per request outcome (R8.1–R8.3):

        1. **Guard first.** If the guard raises
           :class:`~rag_system.sql_lab.guard.SqlLabValidationError`, a single
           ``rejected`` record is persisted and the error re-raised; the
           executor is never called, so a rejected statement never reaches the
           database (R3.10, R4.11, R4.12, R8.3).
        2. **Execute.** If the guard-approved statement fails
           (config/connection/timeout/execution — any
           :class:`~rag_system.sql_lab.errors.SqlLabError`), a single ``error``
           record carrying the measured wall-clock duration and the failure
           detail is persisted and the error re-raised (R8.2).
        3. **Shape + success.** On success the fetched rows are trimmed to
           ``Row_Limit`` with ``truncated`` derived from the overflow (R4.6,
           R4.7), then a single ``success`` record is persisted **before** the
           Result_Set is returned.

        If persisting the audit record itself fails
        (:class:`~rag_system.sql_lab.errors.SqlLabAuditError`), that error
        propagates instead of the Result_Set, so result rows are withheld when
        the request could not be recorded (R8.6).
        """
        # 1. Guard first — a rejection here never reaches the executor.
        try:
            self._guard.validate(sql)
        except SqlLabValidationError as exc:
            self._record(
                user_identity=user_identity,
                sql=sql,
                outcome="rejected",
                error_detail=str(exc),
            )
            raise

        # 2. Execute the approved statement (fetches Row_Limit + 1 rows).
        #    Wall-clock time is measured so a failure still records a duration.
        start = time.perf_counter()
        try:
            result = self._executor.execute(sql)
        except SqlLabError as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            self._record(
                user_identity=user_identity,
                sql=sql,
                outcome="error",
                duration_ms=elapsed_ms,
                error_detail=str(exc),
            )
            raise

        # 3. Shape the Result_Set: trim to Row_Limit and derive truncation.
        row_limit = self._settings.sql_lab_row_limit
        trimmed_rows = result.rows[:row_limit]
        truncated = len(result.rows) > row_limit

        run_result = SqlRunResult(
            columns=result.columns,
            rows=trimmed_rows,
            row_count=len(trimmed_rows),
            duration_ms=result.duration_ms,
            sql=sql,
            truncated=truncated,
        )

        # 4. Record the success outcome BEFORE returning the rows. A persistence
        #    failure surfaces as SqlLabAuditError and propagates, so the caller
        #    withholds the Result_Set when the request could not be recorded
        #    (R8.6).
        self._record(
            user_identity=user_identity,
            sql=sql,
            outcome="success",
            duration_ms=result.duration_ms,
            row_count=run_result.row_count,
        )

        return run_result

    def _record(
        self,
        *,
        user_identity: str,
        sql: str,
        outcome: AuditOutcome,
        duration_ms: int | None = None,
        row_count: int | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Build and persist exactly one audit record for a request outcome.

        The record identifies the requesting user (R8.4), truncates the SQL to
        the audit limit and stamps a UTC timestamp (via
        :meth:`SqlLabAuditRecord.create`, R8.5/R8.1). A persistence failure is
        surfaced by the store as :class:`SqlLabAuditError` and is allowed to
        propagate (R8.6).
        """
        record = SqlLabAuditRecord.create(
            user_identity=user_identity,
            sql=sql,
            outcome=outcome,
            duration_ms=duration_ms,
            row_count=row_count,
            error_detail=error_detail,
        )
        self._audit_store.persist(record)


    def list_schema(self) -> list[SchemaTable]:
        """List tables + columns the viewer role can ``SELECT`` (``GET /sql/schema``).

        Delegates to :class:`~rag_system.sql_lab.schema_lister.SqlLabSchemaLister`,
        which queries ``information_schema`` over the read-only viewer
        connection, restricted to objects the viewer role holds a ``SELECT``
        grant on (R7.1) — so Sensitive_Tables never appear (R7.3). Any
        credential/connection/query failure propagates for the route layer to
        map to an HTTP status code; no partial list is ever returned (R7.4).
        """
        return self._schema_lister.list_schema()


__all__ = ["SqlLabService", "SqlRunResult"]
