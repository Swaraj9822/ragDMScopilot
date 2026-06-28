import logging
import re
import time
import uuid
from datetime import datetime
from functools import lru_cache

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from rag_system.config import get_settings
from rag_system.copilot import DatabaseCopilotService, SqlValidationError
from rag_system.observability_tracing.buffers import BoundedLogBuffer
from rag_system.observability_tracing.flush_workers import LogFlushWorker, TraceFlushWorker
from rag_system.observability_tracing.log_handler import TracePersistingLogHandler
from rag_system.observability_tracing.log_store import (
    LogSearchFilters,
    PostgresLogStore,
)
from rag_system.observability_tracing.models import LogRecordModel, Trace
from rag_system.observability_tracing.retention_scheduler import RetentionScheduler
from rag_system.observability_tracing.trace_store import (
    PostgresTraceStore,
    TraceSearchFilters,
)
from rag_system.models import (
    CopilotQueryRequest,
    CopilotQueryResponse,
    DocumentRecord,
    QueryFeedbackRecord,
    QueryFeedbackRequest,
    QueryRequest,
    QueryResponse,
    QueryTraceRecord,
    UnifiedQueryRequest,
    UnifiedQueryResponse,
)
from rag_system.observability import (
    get_logger,
    metrics,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)
from rag_system.parsing import SUPPORTED_EXTENSIONS
from rag_system.router import AgenticRouter
from rag_system.service import RagService

setup_logging()
logger = get_logger(__name__)

app = FastAPI(title="Production RAG", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@lru_cache
def get_service() -> RagService:
    return RagService(get_settings())


@lru_cache
def get_copilot_service() -> DatabaseCopilotService:
    return DatabaseCopilotService(get_settings())


@lru_cache
def get_router() -> AgenticRouter:
    settings = get_settings()
    rag = get_service()
    try:
        copilot = get_copilot_service()
    except Exception:
        logger.warning("Copilot service unavailable — router will use RAG only")
        copilot = None
    return AgenticRouter(settings, rag, copilot)


@lru_cache
def get_trace_store() -> PostgresTraceStore:
    return PostgresTraceStore(get_settings())


@lru_cache
def get_log_store() -> PostgresLogStore:
    return PostgresLogStore(get_settings())


# ---------------------------------------------------------------------------
# Observability platform startup wiring (R9.1, R17.1)
# ---------------------------------------------------------------------------

_observability_started = False


def _start_observability_platform() -> None:
    """Wire all observability platform background components at app startup.

    This function is idempotent — guarded by the module-level
    ``_observability_started`` flag so repeated calls (e.g. in tests or
    multi-worker scenarios) are no-ops.

    The function:
    1. Creates a :class:`BoundedLogBuffer` for the log handler and log flush
       worker to share.
    2. Attaches a :class:`TracePersistingLogHandler` to the root logger so
       structured log records are captured for persistence.
    3. Starts a :class:`TraceFlushWorker` (drains the span buffer maintained by
       the :func:`get_span_recorder` singleton into the trace store).
    4. Starts a :class:`LogFlushWorker` (drains the log buffer into the log
       store).
    5. Starts a :class:`RetentionScheduler` (periodically enforces retention on
       both stores).

    All flush workers and the scheduler are daemon threads, so they never keep
    the process alive past its natural lifetime. All store writes happen on these
    background threads — never on the request-response path (R9.1, R17.1).
    """
    global _observability_started  # noqa: PLW0603
    if _observability_started:
        return
    _observability_started = True

    settings = get_settings()

    # -- Ensure the observability schema exists before any writer starts --
    # Idempotent (CREATE TABLE/INDEX IF NOT EXISTS), so this is safe to run on
    # every startup and removes the need for a separate manual migration step.
    # Without it, /traces and /logs fail with HTTP 500 because the traces and
    # log_records tables do not exist.
    from rag_system.observability_tracing import schema

    try:
        schema.apply_schema(settings)
    except Exception:  # noqa: BLE001 - never let schema setup crash the API
        logger.exception(
            "Failed to apply observability schema; trace/log persistence and "
            "queries will be unavailable until the database is reachable and "
            "migrated. Copilot and document ingestion are unaffected."
        )

    # -- Span recorder (singleton) provides the span buffer --
    from rag_system.observability_tracing import get_span_recorder

    recorder = get_span_recorder()

    # -- Log buffer shared between the handler and the log flush worker --
    log_buffer = BoundedLogBuffer(
        capacity=settings.log_buffer_capacity,
        metrics=metrics,
    )

    # -- Attach the log handler to the root logger --
    handler = TracePersistingLogHandler(log_buffer)
    logging.getLogger().addHandler(handler)

    # -- Start trace flush worker (spans → trace store, off request path) --
    trace_flush = TraceFlushWorker(
        span_buffer=recorder._span_buffer,
        trace_store=get_trace_store(),
    )
    trace_flush.start()

    # -- Start log flush worker (log records → log store, off request path) --
    log_flush = LogFlushWorker(
        log_buffer=log_buffer,
        log_store=get_log_store(),
    )
    log_flush.start()

    # -- Start retention scheduler --
    retention = RetentionScheduler(
        trace_store=get_trace_store(),
        log_store=get_log_store(),
        trace_retention_hours=settings.trace_retention_hours,
        log_retention_hours=settings.log_retention_hours,
        interval_hours=settings.retention_interval_hours,
    )
    retention.start()

    logger.info("Observability platform started (flush workers + retention scheduler)")


@app.on_event("startup")
async def _on_startup() -> None:
    """FastAPI startup hook — wire the observability platform when tracing is enabled."""
    settings = get_settings()
    if settings.tracing_enabled:
        _start_observability_platform()


# ---------------------------------------------------------------------------
# Trace_Query_Service validation helpers (R7, R8)
# ---------------------------------------------------------------------------

#: A syntactically valid trace_id is a non-empty 32-character lowercase
#: hexadecimal string (R7.3).
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Result-limit bounds for trace search (R8.7).
_MIN_TRACE_LIMIT = 1
_MAX_TRACE_LIMIT = 1000

#: Minimum-duration bounds for trace search, in milliseconds (R8.4, R8.9).
_MIN_DURATION_MS = 0
_MAX_DURATION_MS = 86_400_000

#: Result-limit bounds for log search (R16.8).
_MIN_LOG_LIMIT = 1
_MAX_LOG_LIMIT = 1000


# ---------------------------------------------------------------------------
# Middleware — log every request / response
# ---------------------------------------------------------------------------


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    token = set_trace_id(trace_id)
    logger.info(
        "→ %s %s",
        request.method,
        request.url.path,
        extra={"method": request.method, "path": request.url.path},
    )
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        labels = {
            "method": request.method,
            "path": request.url.path,
            "status_code": "500",
        }
        metrics.increment("rag_http_requests_total", labels)
        metrics.observe("rag_http_request_duration_ms", elapsed_ms, labels)
        logger.error(
            "← %s %s 500 (%.0fms)",
            request.method,
            request.url.path,
            elapsed_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": elapsed_ms,
            },
            exc_info=True,
        )
        reset_trace_id(token)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Trace-Id"] = trace_id
    labels = {
        "method": request.method,
        "path": request.url.path,
        "status_code": str(response.status_code),
    }
    metrics.increment("rag_http_requests_total", labels)
    metrics.observe("rag_http_request_duration_ms", elapsed_ms, labels)
    logger.info(
        "← %s %s %s (%.0fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": elapsed_ms,
        },
    )
    reset_trace_id(token)
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
def root() -> dict[str, object]:
    return {
        "name": "Production RAG",
        "status": "ok",
        "docs": "/docs",
        "endpoints": {
            "health": "/health",
            "metrics": "/metrics",
            "ask": "POST /ask",
            "upload_document": "POST /documents",
            "get_document": "GET /documents/{document_id}",
            "update_document": "PUT /documents/{document_id}",
            "delete_document": "DELETE /documents/{document_id}",
            "query": "POST /query",
            "get_query_trace": "GET /queries/{trace_id}",
            "record_query_feedback": "POST /queries/{trace_id}/feedback",
            "copilot_query": "POST /copilot/query",
        },
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(
        metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.post(
    "/documents",
    response_model=DocumentRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
) -> DocumentRecord:
    content = await _read_document_upload(request, file)

    logger.info(
        "Received document upload: %s (%d bytes)",
        file.filename,
        len(content),
        extra={"file_name": file.filename},
    )

    service = get_service()
    return await service.queue_pdf(file.filename or "document", content)


@app.put(
    "/documents/{document_id}",
    response_model=DocumentRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_document(
    document_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> DocumentRecord:
    content = await _read_document_upload(request, file)

    record = await get_service().update_document(
        document_id,
        file.filename or "document",
        content,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@app.delete("/documents/{document_id}", response_model=DocumentRecord)
def delete_document(document_id: str) -> DocumentRecord:
    record = get_service().delete_document(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


async def _read_document_upload(request: Request, file: UploadFile) -> bytes:
    filename = file.filename or ""
    ext = filename[filename.rfind("."):].lower() if "." in filename else ""
    if not ext or ext not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format: '{ext or '(none)'}'. Supported: {supported}",
        )

    max_upload_bytes = get_settings().max_upload_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            request_size = int(content_length)
        except ValueError:
            request_size = None
        if request_size is not None and request_size > max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Uploaded file is too large. Maximum size is {max_upload_bytes} bytes.",
            )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Uploaded file is too large. Maximum size is {max_upload_bytes} bytes.",
        )
    return content


@app.get("/documents/{document_id}", response_model=DocumentRecord)
def get_document(document_id: str) -> DocumentRecord:
    record = get_service().get_document(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@app.post("/ask", response_model=UnifiedQueryResponse)
def ask(request: UnifiedQueryRequest) -> UnifiedQueryResponse:
    """Unified endpoint — auto-routes to RAG, database copilot, or both."""
    logger.info(
        "Unified query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    try:
        return get_router().query(request)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    logger.info(
        "Query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    return get_service().query(request)


@app.get("/queries/{trace_id}", response_model=QueryTraceRecord)
def get_query_trace(trace_id: str) -> QueryTraceRecord:
    record = get_service().get_query_trace(trace_id)
    if not record:
        raise HTTPException(status_code=404, detail="Query trace not found.")
    return record


@app.post("/queries/{trace_id}/feedback", response_model=QueryFeedbackRecord)
def record_query_feedback(
    trace_id: str,
    feedback: QueryFeedbackRequest,
) -> QueryFeedbackRecord:
    record = get_service().record_query_feedback(trace_id, feedback)
    if not record:
        raise HTTPException(status_code=404, detail="Query trace not found.")
    return record


@app.post("/copilot/query", response_model=CopilotQueryResponse)
def copilot_query(request: CopilotQueryRequest) -> CopilotQueryResponse:
    logger.info(
        "Copilot query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    try:
        return get_copilot_service().query(request)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Trace_Query_Service endpoints (R7, R8)
# ---------------------------------------------------------------------------


@app.get("/traces/{trace_id}")
def get_trace(trace_id: str) -> Trace:
    """Fetch a single trace and all of its spans by trace_id (R7).

    Rejects a malformed trace_id with HTTP 400 (R7.3) and returns HTTP 404 when
    no trace exists for a syntactically valid id (R7.4). On success the trace is
    returned with its spans ordered ascending by start timestamp then span_id and
    the Root_Span's parent set to null (the store guarantees this ordering).
    """
    if not _TRACE_ID_RE.fullmatch(trace_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Malformed trace_id: expected a 32-character lowercase "
                "hexadecimal string."
            ),
        )
    trace = get_trace_store().get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found.")
    return trace


@app.get("/traces")
def search_traces(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    route: str | None = Query(default=None),
    status: str | None = Query(default=None),
    min_duration_ms: int | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> list[Trace]:
    """Search traces by time range, route, status, and minimum duration (R8).

    All supplied filters combine with AND semantics. The time range is inclusive
    on both boundaries (R8.1); route and status are case-sensitive (R8.2, R8.3).
    Results are ordered by start timestamp descending and capped at ``limit``
    (default 100, max 1000 — R8.6, R8.7). An inverted time range (R8.8) or an
    out-of-range ``limit`` / ``min_duration_ms`` (R8.9) is rejected with HTTP 400
    naming the offending parameter, without returning any traces.
    """
    if start is not None and end is not None and end < start:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp range: end must not be earlier than start.",
        )
    if limit is not None and not (_MIN_TRACE_LIMIT <= limit <= _MAX_TRACE_LIMIT):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Parameter 'limit' is out of range: must be between "
                f"{_MIN_TRACE_LIMIT} and {_MAX_TRACE_LIMIT} inclusive."
            ),
        )
    if min_duration_ms is not None and not (
        _MIN_DURATION_MS <= min_duration_ms <= _MAX_DURATION_MS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Parameter 'min_duration_ms' is out of range: must be between "
                f"{_MIN_DURATION_MS} and {_MAX_DURATION_MS} inclusive."
            ),
        )

    filters = TraceSearchFilters(
        start=start,
        end=end,
        route=route,
        status=status,
        min_duration_ms=min_duration_ms,
        limit=limit if limit is not None else 100,
    )
    return get_trace_store().search_traces(filters)


# ---------------------------------------------------------------------------
# Log_Query_Service endpoints (R15, R16)
# ---------------------------------------------------------------------------


@app.get("/logs/{trace_id}")
def get_logs_by_trace(trace_id: str) -> list[LogRecordModel]:
    """Fetch all log records correlated to a trace_id (R15).

    Rejects a malformed trace_id with HTTP 400 (R15.3). For a syntactically valid
    trace_id the matching records are returned ordered by timestamp descending,
    ties broken by descending insertion order (the store guarantees this). When no
    records exist for the trace_id an empty result set is returned with HTTP 200
    (R15.4).
    """
    if not _TRACE_ID_RE.fullmatch(trace_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Malformed trace_id: expected a 32-character lowercase "
                "hexadecimal string."
            ),
        )
    return get_log_store().get_by_trace(trace_id)


@app.get("/logs")
def search_logs(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    level: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> list[LogRecordModel]:
    """Search log records by time range, level, and trace_id (R16).

    All supplied filters combine with AND semantics. The time range is inclusive
    on both boundaries (R16.1); level and trace_id are case-sensitive (R16.2,
    R16.3). Results are ordered by timestamp descending and capped at ``limit``
    (default 100, max 1000 — R16.5, R16.6). An inverted time range (R16.7) or an
    out-of-range ``limit`` (R16.8) is rejected with HTTP 400 naming the offending
    parameter, without returning any records. A valid search that matches nothing
    returns an empty result set with HTTP 200 (R16.9).
    """
    if start is not None and end is not None and end < start:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp range: end must not be earlier than start.",
        )
    if limit is not None and not (_MIN_LOG_LIMIT <= limit <= _MAX_LOG_LIMIT):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Parameter 'limit' is out of range: must be between "
                f"{_MIN_LOG_LIMIT} and {_MAX_LOG_LIMIT} inclusive."
            ),
        )

    filters = LogSearchFilters(
        start=start,
        end=end,
        level=level,
        trace_id=trace_id,
        limit=limit if limit is not None else 100,
    )
    return get_log_store().search(filters)
