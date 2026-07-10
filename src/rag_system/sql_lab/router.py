"""SQL Lab HTTP endpoints (operator-only read-only data explorer).

Slice 1 exposes ``POST /sql/run``: validate a submitted statement with the
secondary :class:`~rag_system.sql_lab.guard.SqlLabGuard`, execute the approved
statement over the dedicated read-only viewer role, and return the shaped
``Result_Set``. The route is operator-gated exactly like ``/replays``,
``/feedback``, and ``/evaluation`` — it reuses the shared ``require_operator``
FastAPI dependency, so an unauthenticated caller gets ``401`` and a
non-operator gets ``403`` before any handler logic runs (R4.2, R4.3). Mount with
``app.include_router(router)`` from the main application.

Error mapping (design "Components and Interfaces → Routes"):

* :class:`~rag_system.sql_lab.guard.SqlLabValidationError` (guard rejection) and
  request-level validation (empty/whitespace, over-length) → ``400`` (R4.11,
  R4.12).
* :class:`~rag_system.sql_lab.errors.SqlLabConfigError` (missing viewer
  credentials) and :class:`~rag_system.sql_lab.errors.SqlLabConnectionError`
  (viewer connection failed) → ``400`` with a keyed, value-free message.
* :class:`~rag_system.sql_lab.errors.SqlLabTimeoutError` → ``504`` (R4.9).
* :class:`~rag_system.sql_lab.errors.SqlLabExecutionError` (other db error) →
  ``400`` carrying the database message (R4.13).
* :class:`~rag_system.sql_lab.errors.SqlLabAuditError` (the request outcome
  could not be recorded) → ``500`` with no result rows (R8.6).

``POST /sql/analyze`` (Slice 4) is operator-gated the same way (R9.1) and
produces a validated ``Chart_Spec`` for a supplied Result_Set. It never mutates
or returns the source Result_Set — analysis is an explicit, separate request,
never auto-run by ``POST /sql/run``. Its error mapping:

* :class:`~rag_system.sql_lab.chart_spec.ChartSpecValidationError` (model output
  failed Chart_Spec schema validation) → ``400``, returning no unvalidated
  content (R9.6).
* :class:`~rag_system.sql_lab.errors.SqlLabAnalysisError` (model unavailable or
  over the 60s budget) → ``503`` indicating analysis could not be completed
  (R9.10).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rag_system.auth import UserPublic, require_operator
from rag_system.config import Settings, get_settings
from rag_system.observability import get_logger
from rag_system.rate_limit import SlidingWindowRateLimiter, rate_limit
from rag_system.sql_lab.analyzer import ChartSpecAnalyzer
from rag_system.sql_lab.chart_spec import ChartSpec, ChartSpecValidationError
from rag_system.sql_lab.errors import (
    SqlLabAnalysisError,
    SqlLabAuditError,
    SqlLabConfigError,
    SqlLabConnectionError,
    SqlLabExecutionError,
    SqlLabTimeoutError,
)
from rag_system.sql_lab.gemini_client import AnalysisMode
from rag_system.sql_lab.guard import SqlLabValidationError
from rag_system.sql_lab.service import SqlLabService, SqlRunResult

logger = get_logger(__name__)

router = APIRouter(prefix="/sql", tags=["sql-lab"])

# Per-process rate limiters for the SQL Lab endpoints, keyed by the configured
# per-minute allowance so a settings reload rebuilds them (mirrors the auth
# limiter). ``None`` when throttling is disabled (allowance 0).
_sql_lab_limiters: dict[int, SlidingWindowRateLimiter | None] = {}


def _get_sql_lab_limiter(settings: Settings) -> SlidingWindowRateLimiter | None:
    rpm = settings.sql_lab_rate_limit_per_minute
    if rpm not in _sql_lab_limiters:
        _sql_lab_limiters[rpm] = (
            SlidingWindowRateLimiter(limit=rpm, window_seconds=60.0) if rpm > 0 else None
        )
    return _sql_lab_limiters[rpm]


def _sql_lab_rate_limit(scope: str):
    """FastAPI dependency list throttling *scope* for a SQL Lab endpoint.

    Resolves the shared limiter lazily on first request; a no-op when the
    per-minute allowance is 0. Keyed by client identifier per :func:`rate_limit`.
    """

    def dependency(request: Request, settings: Settings = Depends(get_settings)) -> None:
        limiter = _get_sql_lab_limiter(settings)
        if limiter is None:
            return
        rate_limit(limiter, scope=scope)(request)

    return [Depends(dependency)]

#: Maximum accepted length of a submitted SQL string (R4.1). Enforced in the
#: handler (rather than as a pydantic field constraint) so an over-length body
#: maps to a ``400`` validation error consistent with the empty/whitespace and
#: guard-rejection paths, instead of FastAPI's default ``422``.
MAX_SQL_LENGTH = 10_000

#: Upper bound on the number of rows accepted in a ``POST /sql/analyze`` body.
#: A Result_Set can carry at most ``sql_lab_row_limit`` rows (max 10000), so this
#: never rejects a legitimate payload; it caps an oversized/abusive body so the
#: request is not held unbounded in memory (the analyzer only forwards a 20-row
#: sample to the model regardless).
MAX_ANALYZE_ROWS = 10_000


class SqlRunRequest(BaseModel):
    """Request body for ``POST /sql/run``.

    ``sql`` must be a non-whitespace string of 1–10000 characters. Length and
    whitespace are validated in the handler so violations surface as ``400``
    (see :data:`MAX_SQL_LENGTH`).
    """

    sql: str = Field(
        description=(
            "The read-only SELECT to execute. 1–10000 characters, "
            "non-whitespace required."
        ),
    )


class SqlRunResponse(BaseModel):
    """The Result_Set returned by ``POST /sql/run``.

    Field names serialize to the camelCase JSON shape the frontend consumes
    (``rowCount``/``durationMs``); the other fields are already camelCase-safe.
    """

    model_config = ConfigDict(populate_by_name=True)

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(serialization_alias="rowCount")
    duration_ms: int = Field(serialization_alias="durationMs")
    sql: str
    truncated: bool

    @classmethod
    def from_result(cls, result: SqlRunResult) -> "SqlRunResponse":
        """Build the response from the service's shaped :class:`SqlRunResult`."""
        return cls(
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            duration_ms=result.duration_ms,
            sql=result.sql,
            truncated=result.truncated,
        )


def get_sql_lab_service(
    settings: Settings = Depends(get_settings),
) -> SqlLabService:
    """Provide the :class:`SqlLabService` for the route.

    Construction is cheap (the guard is built from the sensitive-table denylist
    and the executor only stores settings — no connection is opened until a
    statement is executed), so a fresh service per request is fine. Exposed as a
    dependency so tests can override it.
    """
    return SqlLabService(settings)


def _operator_identity(operator: UserPublic) -> str:
    """Derive the audit user identity from the resolved operator (R8.4).

    The identity recorded in the audit trail is the operator's email — the
    human-readable identity carried by the validated JWT — falling back to the
    subject id if an email is somehow absent.
    """
    return operator.email or operator.id


@router.post(
    "/run",
    response_model=SqlRunResponse,
    dependencies=_sql_lab_rate_limit("sql_run"),
)
def run_sql(
    request: SqlRunRequest,
    operator: UserPublic = Depends(require_operator),
    service: SqlLabService = Depends(get_sql_lab_service),
) -> SqlRunResponse:
    """Validate and execute a single read-only ``SELECT`` and return its rows.

    Operator-only (``require_operator`` also resolves the calling user, whose
    identity is threaded into the audit trail). The guard runs first (a
    rejection never reaches the database), then the approved statement executes
    over the viewer role and the rows are shaped into the Result_Set. Exactly
    one audit record is persisted per outcome; if that record cannot be
    persisted the request returns ``500`` and no result rows (R8.6). Other
    errors map to HTTP status codes per the module docstring.
    """
    sql = request.sql

    # Request-level validation → 400 (kept consistent with guard rejections).
    if not sql.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The submitted SQL is empty or contains only whitespace.",
        )
    if len(sql) > MAX_SQL_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"The submitted SQL exceeds the maximum length of "
                f"{MAX_SQL_LENGTH} characters."
            ),
        )

    try:
        result = service.run(sql, _operator_identity(operator))
    except SqlLabValidationError as exc:
        # Guard rejection — the statement was never executed (R4.11, R4.12).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except (SqlLabConfigError, SqlLabConnectionError) as exc:
        # Missing viewer credentials or a failed viewer connection. The message
        # is keyed and value-free (never contains a credential value).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except SqlLabTimeoutError as exc:
        # Statement timeout exceeded (R4.9).
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        ) from exc
    except SqlLabExecutionError as exc:
        # Any other database error, carrying the db message (R4.13).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except SqlLabAuditError as exc:
        # The outcome could not be recorded; result rows are withheld (R8.6).
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The request could not be recorded, so no results were returned.",
        ) from exc

    return SqlRunResponse.from_result(result)


class SchemaColumnResponse(BaseModel):
    """A single column in the schema listing (``GET /sql/schema``)."""

    name: str
    type: str


class SchemaTableResponse(BaseModel):
    """A table and its columns in the schema listing (``GET /sql/schema``)."""

    name: str
    columns: list[SchemaColumnResponse]


@router.get(
    "/schema",
    response_model=list[SchemaTableResponse],
    dependencies=[Depends(require_operator)],
)
def list_schema(
    service: SqlLabService = Depends(get_sql_lab_service),
) -> list[SchemaTableResponse]:
    """List tables + columns the viewer role can ``SELECT`` from ``information_schema``.

    Operator-only (same ``require_operator`` dependency as ``POST /sql/run`` — an
    unauthenticated caller gets ``401`` and a non-operator ``403`` before any
    handler logic runs, R7.2). The listing is restricted at the database level
    to objects the viewer role holds a ``SELECT`` grant on, so Sensitive_Tables
    never appear (R7.1, R7.3). On any failure the whole request errors and no
    partial list is returned (R7.4):

    * :class:`~rag_system.sql_lab.errors.SqlLabConfigError` (missing viewer
      credentials) and
      :class:`~rag_system.sql_lab.errors.SqlLabConnectionError` (viewer
      connection failed) → ``400`` with a keyed, value-free message.
    * :class:`~rag_system.sql_lab.errors.SqlLabExecutionError` (the
      ``information_schema`` query failed) → ``400`` indicating the schema
      listing could not be retrieved.
    """
    try:
        tables = service.list_schema()
    except (SqlLabConfigError, SqlLabConnectionError) as exc:
        # Missing viewer credentials or a failed viewer connection. The message
        # is keyed and value-free (never contains a credential value).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except SqlLabExecutionError as exc:
        # The information_schema query failed / db unreachable mid-query (R7.4).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The schema listing could not be retrieved.",
        ) from exc

    return [
        SchemaTableResponse(
            name=table.name,
            columns=[
                SchemaColumnResponse(name=column.name, type=column.type)
                for column in table.columns
            ],
        )
        for table in tables
    ]


class SqlAnalyzeRequest(BaseModel):
    """Request body for ``POST /sql/analyze``.

    Carries the source Result_Set to analyze — the same camelCase shape the
    frontend receives from ``POST /sql/run`` (``columns``, ``rows``,
    ``rowCount``, ``durationMs``, ``sql``, ``truncated``) — plus an optional
    analysis ``mode`` (``"default"`` → Gemini Flash, ``"deep"`` → Gemini Pro;
    R9.8/R9.9). Only the column names, inferred types, row count, and a bounded
    row sample are ever forwarded to the model (the analyzer enforces R9.2/R9.3);
    the route never mutates or echoes back the source Result_Set (R9.6/R9.10).

    ``durationMs``, ``sql``, and ``truncated`` are accepted for contract fidelity
    but default to empty/zero because they play no part in analysis.

    Fields are named in camelCase to match the JSON contract directly rather than
    via pydantic ``alias``: FastAPI/Pydantic emits an ``UnsupportedFieldAttribute``
    warning for ``Field(alias=...)`` during request-body field extraction here,
    so declaring the wire names as the field names avoids that warning debt
    (which would break a warnings-as-errors CI) while keeping the exact same
    contract. This mirrors the camelCase fields already used in
    :mod:`rag_system.sql_lab.chart_spec` (e.g. ``xColumn``).
    """

    model_config = ConfigDict(extra="ignore")

    columns: list[str]
    rows: list[dict[str, Any]] = Field(max_length=MAX_ANALYZE_ROWS)
    rowCount: int  # noqa: N815 - camelCase mirrors the JSON contract
    durationMs: int = 0  # noqa: N815 - camelCase mirrors the JSON contract
    sql: str = Field(default="")
    truncated: bool = Field(default=False)
    mode: AnalysisMode = Field(
        default="default",
        description=(
            "Analysis mode: 'default' uses the Gemini Flash model, 'deep' uses "
            "the Gemini Pro model."
        ),
    )


def get_chart_spec_analyzer(
    settings: Settings = Depends(get_settings),
) -> ChartSpecAnalyzer:
    """Provide the :class:`ChartSpecAnalyzer` for the analyze route.

    Defaults to a :class:`ChartSpecAnalyzer` built from ``Settings`` (which owns
    a dedicated schema-constrained Gemini client). Exposed as a dependency so
    tests can override it with a stub that returns a canned Chart_Spec or raises
    a mapped error, without the ``google-genai`` dependency or a live model.
    """
    return ChartSpecAnalyzer(settings)


@router.post(
    "/analyze",
    response_model=ChartSpec,
    dependencies=_sql_lab_rate_limit("sql_analyze"),
)
def analyze_result_set(
    request: SqlAnalyzeRequest,
    operator: UserPublic = Depends(require_operator),
    analyzer: ChartSpecAnalyzer = Depends(get_chart_spec_analyzer),
) -> ChartSpec:
    """Produce a validated declarative ``Chart_Spec`` for a Result_Set.

    Operator-only (same ``require_operator`` dependency as ``POST /sql/run`` — an
    unauthenticated caller gets ``401`` and a non-operator ``403`` before any
    data reaches the language model, R9.1). Analysis is an explicit, separate
    request — it is never auto-run as part of ``POST /sql/run``.

    The route hands only the source Result_Set (from which the analyzer forwards
    a bounded sample) to :meth:`ChartSpecAnalyzer.analyze`, then returns the
    validated Chart_Spec. It never mutates or returns the source Result_Set;
    the source rows the caller holds are left unchanged (R9.6, R9.10). Error
    mapping:

    * :class:`~rag_system.sql_lab.chart_spec.ChartSpecValidationError` (the model
      output failed Chart_Spec schema validation) → ``400``; no unvalidated
      content is returned (R9.6).
    * :class:`~rag_system.sql_lab.errors.SqlLabAnalysisError` (the model was
      unavailable or exceeded the 60s budget) → ``503`` indicating analysis
      could not be completed (R9.10).
    """
    result_set = {
        "columns": request.columns,
        "rows": request.rows,
        "rowCount": request.rowCount,
    }

    try:
        return analyzer.analyze(result_set, request.mode)
    except ChartSpecValidationError as exc:
        # The model output failed schema validation; return no unvalidated
        # content and leave the source Result_Set unchanged (R9.6).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except SqlLabAnalysisError as exc:
        # The model was unavailable or exceeded the 60s budget (R9.10).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


__all__ = [
    "router",
    "SqlRunRequest",
    "SqlRunResponse",
    "SchemaTableResponse",
    "SchemaColumnResponse",
    "SqlAnalyzeRequest",
    "get_sql_lab_service",
    "get_chart_spec_analyzer",
]
