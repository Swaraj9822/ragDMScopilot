import asyncio
import logging
import re
import secrets
import threading
import time
import uuid
import json as _json
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.routing import APIRoute

from rag_system.auth import apply_schema as apply_auth_schema
from rag_system.auth import get_current_user
from rag_system.auth import require_operator
from rag_system.auth import router as auth_router
from rag_system.auth.models import UserPublic
from rag_system.config import Settings, get_settings
from rag_system.conversation import ConversationManager
from rag_system.copilot import DatabaseCopilotService, SqlValidationError
from rag_system.observability_tracing import get_span_recorder
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
    AbstentionResponse,
    ActivationEvent,
    AIConfigCreateRequest,
    AIConfigRollbackRequest,
    AIConfigurationVersion,
    BenchmarkCase,
    ClarificationPrompt,
    ClarificationReplyRequest,
    ConversationRecord,
    CopilotQueryRequest,
    CopilotQueryResponse,
    CorpusPage,
    CorpusSnapshotSummary,
    CreateCorpusSnapshotRequest,
    CreateCorpusSnapshotResponse,
    DocumentHistory,
    DocumentRecord,
    DocumentStatus,
    EvaluationRunDetail,
    EvaluationRunSummary,
    FeedbackClassifyRequest,
    FeedbackInboxPage,
    FeedbackReviewRecord,
    KnowledgeGapMap,
    ReviewStatus,
    QueryFeedbackRecord,
    QueryFeedbackRequest,
    QueryRequest,
    QueryResponse,
    QueryTraceRecord,
    ReplayRun,
    ReplayRunRequest,
    TraceDiagnosis,
    UnifiedQueryRequest,
    UnifiedQueryResponse,
)
from rag_system.observability import (
    get_logger,
    metrics,
    reset_token_counter,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)
from rag_system.parsing import SUPPORTED_EXTENSIONS
from rag_system.router import AgenticRouter
from rag_system.clarification import (
    ClarificationInvalidOrExpiredError,
    ClarificationReplyProcessor,
    ClarificationReplyRequiredError,
    ClarificationStore,
)
from rag_system.service import DocumentVersionNotFoundError, RagService

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — set up auth, then wire observability if enabled.

    Replaces the deprecated ``@app.on_event("startup")`` hook. Startup work runs
    before ``yield``; nothing needs explicit teardown after it because the flush
    workers and retention scheduler are daemon threads that stop with the
    process.
    """
    settings = get_settings()

    # Authentication setup: validate configuration early and ensure the users
    # table exists. A misconfigured secret should fail loudly at boot, not on
    # the first login.
    if settings.auth_enabled:
        settings.require_jwt_secret()
        try:
            apply_auth_schema(settings)
        except Exception:  # noqa: BLE001 - never let schema setup crash boot
            logger.exception(
                "Failed to apply auth schema; registration and login will be "
                "unavailable until the database is reachable and migrated."
            )

        # The /metrics scrape endpoint is only token-gated when RAG_METRICS_TOKEN
        # is set; otherwise it stays open (backward compatible). With auth on,
        # an open metrics endpoint is almost certainly unintended, so surface it
        # at boot as a conscious choice rather than a forgotten default.
        if not settings.metrics_token:
            logger.warning(
                "RAG_METRICS_TOKEN is unset while auth is enabled: the /metrics "
                "endpoint is publicly reachable. Set RAG_METRICS_TOKEN or block "
                "/metrics at the reverse proxy."
            )

        # Prune expired refresh tokens periodically so the table does not grow
        # ~1 row per login/refresh forever (the auth flow never deletes rows).
        _start_refresh_token_cleanup(settings)

    if settings.tracing_enabled:
        _start_observability_platform()

    yield


app = FastAPI(title="Production RAG", version="0.1.0", lifespan=lifespan)

# Restrict cross-origin access to the configured console origin(s). A wildcard
# with credentials is invalid in browsers and unsafe, so origins are explicit
# and configurable via RAG_CORS_ALLOW_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
)

# Authentication endpoints (/auth/register, /auth/login, /auth/me).
app.include_router(auth_router)


@lru_cache
def get_service() -> RagService:
    return RagService(get_settings())


@lru_cache
def get_copilot_service() -> DatabaseCopilotService:
    return DatabaseCopilotService(get_settings())


@lru_cache
def get_conversations() -> ConversationManager:
    """Server-side multi-turn conversation store + follow-up rewriter.

    Shares the RAG service's S3 artifact store so conversations live alongside
    documents and query traces in the same bucket.
    """
    return ConversationManager(store=get_service().artifact_store, settings=get_settings())


@lru_cache
def get_router() -> AgenticRouter:
    settings = get_settings()
    rag = get_service()
    try:
        copilot = get_copilot_service()
        # Force boot-time validation (schema catalog + DB config) so a
        # misconfigured copilot degrades to RAG-only here instead of failing
        # the first copilot query with a 500. Everything on the service is
        # otherwise lazy, so without this the except below is unreachable.
        copilot.validate_ready()
    except Exception:
        logger.warning(
            "Copilot service unavailable — router will use RAG only", exc_info=True
        )
        copilot = None
    return AgenticRouter(settings, rag, copilot, conversations=get_conversations())


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

#: Idempotency guard + reference holder for the refresh-token cleanup daemon.
_refresh_cleanup_started = False
_refresh_cleanup_scheduler = None


def _start_refresh_token_cleanup(settings: Settings) -> None:
    """Start the background daemon that prunes expired refresh tokens.

    Idempotent (guarded by ``_refresh_cleanup_started``). The scheduler is a
    daemon thread that periodically calls ``delete_expired`` on the refresh-token
    store; a reference is held module-side so it is not garbage collected. Best
    effort — construction never touches the database (the store is lazy), and a
    failed sweep is logged without crashing the thread.
    """
    global _refresh_cleanup_started, _refresh_cleanup_scheduler  # noqa: PLW0603
    if _refresh_cleanup_started:
        return
    _refresh_cleanup_started = True

    from rag_system.auth.cleanup import RefreshTokenCleanupScheduler
    from rag_system.auth.refresh_store import PostgresRefreshTokenStore

    scheduler = RefreshTokenCleanupScheduler(
        PostgresRefreshTokenStore(settings),
        interval_hours=getattr(settings, "retention_interval_hours", 24.0),
    )
    scheduler.start()
    _refresh_cleanup_scheduler = scheduler
    logger.info("Refresh-token cleanup scheduler started")


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


# ---------------------------------------------------------------------------
# Trace_Query_Service validation helpers (R7, R8)
# ---------------------------------------------------------------------------

#: A syntactically valid trace_id is a non-empty 32-character lowercase
#: hexadecimal string (R7.3).
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Single source of truth for the streaming endpoint path. Referenced by both
#: the route decorator and the middleware's tracing branch below, so renaming
#: the route updates both at once instead of silently changing tracing behavior
#: if only one string were edited.
_ASK_STREAM_PATH = "/ask/stream"


def _resolve_inbound_trace_id(header_value: str | None) -> str:
    """Return a valid trace id for an incoming request.

    A client-supplied ``X-Trace-Id`` is trusted only when it is syntactically
    valid (32 lowercase hex chars) — the same shape ``GET /traces/{id}`` accepts.
    A malformed or uppercase value would otherwise produce a trace that can
    never be fetched by id, so we regenerate one instead of adopting it.
    """
    if header_value and _TRACE_ID_RE.fullmatch(header_value):
        return header_value
    return uuid.uuid4().hex


def verify_metrics_access(request: Request) -> None:
    """Gate ``/metrics`` behind a dedicated bearer token when one is configured.

    Prometheus scrapers don't carry user JWTs, so when ``RAG_METRICS_TOKEN`` is
    set the scrape job authenticates with that token instead. When it is unset
    the endpoint stays open (backward compatible); set it in production, or block
    ``/metrics`` at the reverse proxy, so route latencies and model ids are not
    publicly readable.
    """
    token = get_settings().metrics_token
    if not token:
        return
    provided = request.headers.get("Authorization", "")
    expected = f"Bearer {token}"
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid metrics token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

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

#: Non-stream routes that produce a query trace which should record the
#: producing AI configuration version (R9.1). ``/ask/stream`` stamps its own
#: trace inside the response generator. Defined here, above the middleware that
#: reads it, so the dependency is visible at the point of use.
_ANSWER_TRACE_PATHS = frozenset({"/ask", "/query"})


def _stamp_trace_config(span: object) -> None:
    """Resolve the active AI configuration and stamp it on *span* (R9.1, R9.11).

    Records the producing ``ai_configuration_version_id`` and its redacted
    settings on the trace's root span so downstream consumers (feedback context,
    replay comparison, trace diagnosis) can attribute an outcome to a config
    version. Best-effort: any failure resolves to the ``unresolved`` sentinel
    (R9.2) and never breaks the request.
    """
    try:
        from rag_system.ai_config import AIConfigResolver, AIConfigurationStore
        from rag_system.observability_tracing import build_trace_config_payload

        resolver = AIConfigResolver(AIConfigurationStore(_get_artifact_store()))
        resolved = resolver.resolve()
        payload = build_trace_config_payload(resolved)
        get_span_recorder().set_trace_config(
            span,
            ai_configuration_version_id=payload["ai_configuration_version_id"],
            resolved_settings=payload["resolved_settings"],
        )
    except Exception:  # noqa: BLE001 - never let tracing break the answer path
        logger.warning("Failed to stamp AI configuration on trace", exc_info=True)


def _metric_path_label(request: Request) -> str:
    """Return a bounded ``path`` label for HTTP metrics.

    The matched route's *template* (e.g. ``/documents/{document_id}``) is used
    instead of the concrete request path (``/documents/3f2a...``) so that
    per-id/per-uuid paths do not mint a brand-new Prometheus label set on every
    request. Left unbounded, the in-process registry would grow forever (a slow
    memory leak) and the exposition would be swamped by one-off series.

    Starlette resolves the route during ``call_next`` and stores it on the same
    ``scope`` dict this ``request`` wraps, so this must be read *after*
    ``call_next`` returns. When no route matched (404s, or an error raised
    before routing completed) a fixed sentinel is returned, which likewise keeps
    cardinality bounded. Structured logs continue to use the real path.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "__unmatched__"


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    trace_id = _resolve_inbound_trace_id(request.headers.get("X-Trace-Id"))
    token = set_trace_id(trace_id)
    # Begin a fresh per-request LLM token tally so any generation triggered by
    # this request (including parallel hybrid branches) is summed for its trace.
    reset_token_counter()
    logger.info(
        "→ %s %s",
        request.method,
        request.url.path,
        extra={"method": request.method, "path": request.url.path},
    )
    try:
        # The streaming endpoint's response body is consumed by the server
        # *after* this middleware returns, so wrapping it in start_trace here
        # would close the trace before the streaming work runs. /ask/stream
        # opens and owns its own trace inside the response generator instead.
        if request.url.path == _ASK_STREAM_PATH:
            response = await call_next(request)
        else:
            # Open the request's Root_Span so the call is captured as a trace in
            # the observability platform (R1.1). The recorder adopts the
            # trace_id we just established, times the request, marks the root
            # error if call_next raises, and enqueues the trace for off-path
            # persistence.
            with get_span_recorder().start_trace(
                trace_id=trace_id, route=request.url.path, is_root_http=True
            ) as root_span:
                if request.url.path in _ANSWER_TRACE_PATHS:
                    _stamp_trace_config(root_span)
                response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        labels = {
            "method": request.method,
            "path": _metric_path_label(request),
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
        "path": _metric_path_label(request),
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


@lru_cache(maxsize=1)
def _endpoint_catalog() -> tuple[str, ...]:
    """Build the self-describing endpoint listing once.

    Routes are fixed after startup, so the catalog is computed on first request
    and cached instead of re-derived from ``app.routes`` on every call to
    ``root()``.
    """
    return tuple(
        sorted(
            f"{method} {route.path}"
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in (route.methods or set())
            if method not in ("HEAD", "OPTIONS")
        )
    )


@app.get("/", dependencies=[Depends(get_current_user)])
def root() -> dict[str, object]:
    """Service metadata plus a live, self-describing endpoint listing.

    The endpoint list is generated from the router so it can never drift from
    the actual routes (the previous hand-maintained catalog had already fallen
    out of sync). It is computed once and cached (see ``_endpoint_catalog``).
    See ``/docs`` for full request/response schemas.
    """
    return {
        "name": "Production RAG",
        "status": "ok",
        "docs": "/docs",
        "endpoints": list(_endpoint_catalog()),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(verify_metrics_access)],
)
def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(
        metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.post(
    "/documents",
    response_model=DocumentRecord,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(get_current_user)],
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
    dependencies=[Depends(get_current_user)],
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


@app.delete(
    "/documents/{document_id}",
    response_model=DocumentRecord,
    dependencies=[Depends(get_current_user)],
)
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


@app.get(
    "/documents/{document_id}",
    response_model=DocumentRecord,
    dependencies=[Depends(get_current_user)],
)
def get_document(document_id: str) -> DocumentRecord:
    record = get_service().get_document(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@app.get(
    "/documents",
    response_model=list[DocumentRecord],
    dependencies=[Depends(get_current_user)],
)
def list_documents() -> list[DocumentRecord]:
    """List all uploaded documents."""
    return get_service().list_documents()


@app.get(
    "/documents/{document_id}/versions",
    response_model=DocumentHistory,
    dependencies=[Depends(get_current_user)],
)
def get_document_versions(document_id: str) -> DocumentHistory:
    """Return a Document's version history and ingestion events (R5.7).

    Versions and events are ordered by ingestion timestamp, most recent first.
    """
    history = get_service().get_document_history(document_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return history


@app.post(
    "/documents/{document_id}/versions/{version}/restore",
    response_model=DocumentRecord,
    dependencies=[Depends(require_operator)],
)
def restore_document_version(document_id: str, version: str) -> DocumentRecord:
    """Restore a previous Document_Version as the Active_Version (R5.8-R5.11).

    Operator-only. Re-indexes from retained source content when the version's
    vectors were cleaned up, then flips the active pointer. An unknown version
    yields ``version_not_found`` (404) with the active version unchanged.
    """
    try:
        record = get_service().restore_version(document_id, version)
    except DocumentVersionNotFoundError:
        raise HTTPException(status_code=404, detail="version_not_found")
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@app.post(
    "/ask",
    dependencies=[Depends(get_current_user)],
)
def ask(request: UnifiedQueryRequest) -> UnifiedQueryResponse | ClarificationPrompt | AbstentionResponse:
    """Unified endpoint — auto-routes to RAG, database copilot, or both.

    The answer path: classify → clarification gate → retrieval gates →
    generate + claim mapping → abstention gates.

    Returns one of:
    - ``UnifiedQueryResponse`` with claims + evidence on success.
    - ``ClarificationPrompt`` when the question is ambiguous (R2.1).
    - ``AbstentionResponse`` when the system lacks sufficient evidence (R3).
    """
    logger.info(
        "Unified query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    try:
        result = get_router().query(request)
        if isinstance(result, UnifiedQueryResponse) and not request.include_sql:
            result.sql = None
            result.rows = []
        return result
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _format_sse(event: dict) -> str:
    """Serialize an event dict as a Server-Sent Event frame."""
    payload = _json.dumps(event, default=str, ensure_ascii=False)
    return f"event: {event['type']}\ndata: {payload}\n\n"


@app.post(_ASK_STREAM_PATH, dependencies=[Depends(get_current_user)])
async def ask_stream(request: UnifiedQueryRequest, http_request: Request) -> StreamingResponse:
    """Streaming variant of /ask — holds answer content until gates pass.

    Emits Server-Sent Events for stage progress (``classify``, ``retrieve``,
    ``generate``, ``verify``) for liveness but **holds answer content** — it
    does not forward generated tokens — until the abstention gates and
    claim-verification have run. The stream ends with exactly **one terminal
    event** carrying one of:

    - The answer with claims/evidence (``kind: "answer"``).
    - A ``Clarification_Prompt`` (``kind: "clarification"``).
    - An ``Abstention_Response`` with no answer content (``kind: "abstention"``).

    A post-generation abstention therefore leaks no tokens (R3.7).

    Errors are delivered as an ``error`` event rather than an HTTP status, since
    the response has already started streaming.

    The synchronous router generator is run in a single dedicated thread so the
    whole request shares one contextvars context (trace id, span stack, token
    tally).
    """
    logger.info(
        "Unified streaming query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    # Adopt the caller's trace id (the console always sends one) so the trace is
    # clickable from the answer; regenerate when it is missing or malformed so a
    # bad header can't produce an unfetchable trace.
    trace_id = _resolve_inbound_trace_id(http_request.headers.get("X-Trace-Id"))
    router = get_router()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    # Set when the client disconnects so the producer stops the (token-burning)
    # pipeline instead of running it to completion for nobody.
    stop_event = threading.Event()

    def produce() -> None:
        # Runs in one fresh thread → one stable contextvars context for the
        # entire stream, so the trace, span stack, and token tally are coherent.
        set_trace_id(trace_id)
        reset_token_counter()
        recorder = get_span_recorder()

        def emit(event: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, _format_sse(event))

        try:
            with recorder.start_trace(
                trace_id=trace_id, route=_ASK_STREAM_PATH, is_root_http=True
            ) as root_span:
                _stamp_trace_config(root_span)
                for event in router.query_stream(request):
                    if stop_event.is_set():
                        logger.info(
                            "Client disconnected; aborting stream production",
                            extra={"trace_id": trace_id},
                        )
                        metrics.increment("rag_stream_client_disconnect_total")
                        break
                    emit(event)
        except SqlValidationError as exc:
            emit({"type": "error", "detail": str(exc)})
        except (FileNotFoundError, RuntimeError) as exc:
            emit({"type": "error", "detail": str(exc)})
        except Exception:  # pragma: no cover - defensive
            logger.exception("Streaming query failed")
            emit({"type": "error", "detail": "Internal error while streaming the answer."})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    threading.Thread(target=produce, name="ask-stream", daemon=True).start()

    async def event_stream():
        try:
            while True:
                # Poll for client disconnect even while waiting for the next
                # event, so a long LLM call for a gone client is cut short.
                if await http_request.is_disconnected():
                    stop_event.set()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item is sentinel:
                    break
                yield item
        finally:
            # Whether we broke out on disconnect or the generator was closed by
            # the server, tell the producer to stop.
            stop_event.set()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering so events flush immediately (e.g. nginx).
            "X-Accel-Buffering": "no",
            "X-Trace-Id": trace_id,
        },
    )


# ---------------------------------------------------------------------------
# Clarification reply endpoint (R2.4–R2.8)
# ---------------------------------------------------------------------------


@app.post(
    "/ask/clarify",
    dependencies=[Depends(get_current_user)],
)
def ask_clarify(
    request: ClarificationReplyRequest,
) -> UnifiedQueryResponse | AbstentionResponse:
    """Process a reply to a previously issued clarification (R2.4–R2.8).

    Validates the clarification_id (existence + expiry, R2.5) and that the reply
    is non-empty (R2.6), then re-runs the answer path with the combined question
    scoped to the clarification record's document scope. The ambiguous branch is
    disabled so at most one clarification is ever issued per original question
    (R2.7). If still unresolved after the reply → abstention (R2.8).

    Errors:
    - 400 ``clarification_invalid_or_expired`` — unknown or expired id (R2.5).
    - 400 ``clarification_reply_required`` — empty/whitespace-only reply (R2.6).
    """
    logger.info(
        "Clarification reply received (id=%s, reply_len=%d)",
        request.clarification_id,
        len(request.reply),
        extra={
            "clarification_id": request.clarification_id,
            "reply_len": len(request.reply),
        },
    )
    router = get_router()
    store = _get_clarification_store(router)

    def answer_path(*, question: str, document_scope: list[str] | None):
        """Re-run the answer path with clarification disabled (R2.7)."""
        req = UnifiedQueryRequest(question=question, document_ids=document_scope)
        return router.query(req, allow_clarification=False)

    processor = ClarificationReplyProcessor(store=store, answer_path=answer_path)
    try:
        outcome = processor.process(
            clarification_id=request.clarification_id, reply=request.reply
        )
    except ClarificationInvalidOrExpiredError:
        raise HTTPException(
            status_code=400, detail="clarification_invalid_or_expired"
        )
    except ClarificationReplyRequiredError:
        raise HTTPException(
            status_code=400, detail="clarification_reply_required"
        )
    return outcome


def _get_clarification_store(router: AgenticRouter) -> ClarificationStore:
    """Resolve the clarification store from the router or build one.

    Falls back to creating a store from the RAG service's artifact store if the
    router has not yet lazily initialized one.
    """
    store = router._clarification_store()
    if store is not None:
        return store
    # Fallback: build from the RAG service artifact store.
    service = get_service()
    settings = get_settings()
    return ClarificationStore(service.artifact_store, settings)


@app.post(
    "/query",
    response_model=QueryResponse,
    dependencies=[Depends(get_current_user)],
)
def query(request: QueryRequest) -> QueryResponse:
    logger.info(
        "Query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    return get_service().query(request)


@app.get(
    "/queries/{trace_id}",
    response_model=QueryTraceRecord,
    dependencies=[Depends(get_current_user)],
)
def get_query_trace(trace_id: str) -> QueryTraceRecord:
    record = get_service().get_query_trace(trace_id)
    if not record:
        raise HTTPException(status_code=404, detail="Query trace not found.")
    return record


@app.post(
    "/queries/{trace_id}/feedback",
    response_model=QueryFeedbackRecord,
    dependencies=[Depends(get_current_user)],
)
def record_query_feedback(
    trace_id: str,
    feedback: QueryFeedbackRequest,
) -> QueryFeedbackRecord:
    record = get_service().record_query_feedback(trace_id, feedback)
    if not record:
        raise HTTPException(status_code=404, detail="Query trace not found.")
    return record


@app.get(
    "/conversations/{conversation_id}",
    response_model=ConversationRecord,
    dependencies=[Depends(get_current_user)],
)
def get_conversation(conversation_id: str) -> ConversationRecord:
    """Fetch a stored conversation and its turns.

    Lets the console rehydrate a session (history + rewritten queries + document
    scope) after a reload, and makes the server-side state inspectable.
    """
    record = get_conversations().load(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return record


@app.post(
    "/conversations/{conversation_id}/forget",
    response_model=ConversationRecord,
    dependencies=[Depends(get_current_user)],
)
def forget_conversation(conversation_id: str) -> ConversationRecord:
    """Clear a conversation's accumulated context, preserving its document scope.

    "Forget context" — subsequent follow-ups stop referencing earlier turns, but
    the conversation id and selected-document scope carry on. Use "start new
    topic" on the client (drop the conversation id) to begin a fresh one instead.
    """
    record = get_conversations().forget(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return record


@app.post(
    "/copilot/query",
    response_model=CopilotQueryResponse,
    dependencies=[Depends(get_current_user)],
)
def copilot_query(request: CopilotQueryRequest) -> CopilotQueryResponse:
    logger.info(
        "Copilot query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    try:
        result = get_copilot_service().query(request)
        if not request.include_sql:
            result.sql = None
            result.rows = []
        return result
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Trace_Query_Service endpoints (R7, R8)
# ---------------------------------------------------------------------------


@app.get("/traces/{trace_id}", dependencies=[Depends(get_current_user)])
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


@app.get("/traces", dependencies=[Depends(get_current_user)])
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


@app.get("/logs/{trace_id}", dependencies=[Depends(get_current_user)])
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


@app.get("/logs", dependencies=[Depends(get_current_user)])
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


# ---------------------------------------------------------------------------
# Replay and compare lab (R8)
# ---------------------------------------------------------------------------


def _get_replay_service():
    """Lazily construct the ReplayService for replay endpoints."""
    from rag_system.replay import ReplayService

    store = _get_artifact_store()
    return ReplayService(store, config_store=store)


def _get_artifact_store():
    """Return the shared S3ArtifactStore."""
    from rag_system.storage import S3ArtifactStore

    settings = get_settings()
    return S3ArtifactStore(settings)


@app.post(
    "/replays",
    response_model=ReplayRun,
    status_code=201,
    dependencies=[Depends(require_operator)],
)
def create_replay_run(request: ReplayRunRequest) -> ReplayRun:
    """Initiate a replay run under an approved AI configuration (R8.1–R8.4).

    Operator-only. Validates the referenced AI configuration version is approved
    (with prompt/model drawn from it), retrieval params are within range, and the
    corpus snapshot exists. On success creates a ``queued`` run and returns it
    with its id, without blocking on execution (R8.2).
    """
    from rag_system.replay import ReplayValidationError

    service = _get_replay_service()
    try:
        run = service.create_replay_run(request)
    except ReplayValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    return run


@app.get(
    "/replays/{replay_run_id}",
    response_model=ReplayRun,
    dependencies=[Depends(require_operator)],
)
def get_replay_run(replay_run_id: str) -> ReplayRun:
    """Return the current state of a Replay_Run (R8.10).

    Operator-only. Returns the full :class:`ReplayRun` including its current
    ``state``, the original request, and (when completed) the result.
    """
    from rag_system.storage import replay_run_key

    store = _get_artifact_store()
    payload = store.get_json(replay_run_key(replay_run_id))
    if payload is None:
        raise HTTPException(status_code=404, detail="Replay run not found.")
    return ReplayRun.model_validate(payload)


@app.post(
    "/replays/{replay_run_id}/cancel",
    response_model=ReplayRun,
    dependencies=[Depends(require_operator)],
)
def cancel_replay_run(replay_run_id: str) -> ReplayRun:
    """Cancel a queued or running Replay_Run (R8.9).

    Operator-only. Sets ``cancel_requested = true`` and transitions the run to
    ``cancelled`` with no results. Cancelling a run already in a terminal state
    (``completed``, ``failed``, ``cancelled``) is a no-op — the run is returned
    unchanged. The worker checks the flag at stage boundaries.
    """
    from rag_system.replay import ReplayValidationError

    service = _get_replay_service()
    try:
        return service.cancel_replay_run(replay_run_id)
    except ReplayValidationError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Replay run not found.") from exc
        raise HTTPException(status_code=400, detail=exc.code) from exc


@app.post(
    "/corpus-snapshots",
    response_model=CreateCorpusSnapshotResponse,
    status_code=201,
    dependencies=[Depends(require_operator)],
)
def create_corpus_snapshot(request: CreateCorpusSnapshotRequest) -> CreateCorpusSnapshotResponse:
    """Capture the current active-version manifest as an immutable CorpusSnapshot (R8.1, R8.6).

    Operator-only. Returns the minted ``corpus_snapshot_id`` (201 Created).

    Accepts an optional ``document_ids`` subset scope: when provided, only those
    documents are included in the manifest. When omitted, all documents with an
    active version are captured.

    Accepts an optional ``sql_fixture`` to capture SQL result rows alongside the
    snapshot.
    """
    from rag_system.replay import ReplaySnapshotStore

    service = get_service()
    store = service.artifact_store

    # Gather the active-version manifest from the current corpus.
    all_documents = service.list_documents()

    # Apply optional document-subset scope.
    if request.document_ids is not None:
        scope_set = set(request.document_ids)
        all_documents = [doc for doc in all_documents if doc.id in scope_set]

    # Build the manifest: only documents with an active version.
    manifest: list[tuple[str, str]] = [
        (doc.id, doc.active_version)
        for doc in all_documents
        if doc.active_version is not None
    ]

    # Create the immutable snapshot.
    snapshot_store = ReplaySnapshotStore(store)
    snapshot = snapshot_store.create_snapshot(manifest)

    # Optionally capture the SQL result fixture alongside.
    if request.sql_fixture is not None:
        snapshot_store.create_sql_fixture(
            corpus_snapshot_id=snapshot.corpus_snapshot_id,
            sql=request.sql_fixture.sql,
            rows=request.sql_fixture.rows,
        )

    return CreateCorpusSnapshotResponse(
        corpus_snapshot_id=snapshot.corpus_snapshot_id,
    )


@app.get(
    "/corpus-snapshots",
    response_model=list[CorpusSnapshotSummary],
    dependencies=[Depends(require_operator)],
)
def list_corpus_snapshots() -> list[CorpusSnapshotSummary]:
    """List existing CorpusSnapshots (id + created_at + manifest size) (R8.1).

    Operator-only. Returns all snapshots sorted by creation time (newest first)
    so an operator can pick one when initiating a replay.
    """
    from rag_system.replay import ReplaySnapshotStore

    service = get_service()
    store = service.artifact_store

    # List all snapshot keys from storage.
    keys = store.list_corpus_snapshot_keys()

    # Load each snapshot record.
    snapshot_store = ReplaySnapshotStore(store)
    snapshots = snapshot_store.list_snapshots(keys)

    # Build summaries sorted by created_at descending (newest first).
    summaries = [
        CorpusSnapshotSummary(
            corpus_snapshot_id=s.corpus_snapshot_id,
            created_at=s.created_at,
            manifest_size=len(s.manifest),
        )
        for s in snapshots
    ]
    summaries.sort(key=lambda s: s.created_at, reverse=True)

    return summaries


# ---------------------------------------------------------------------------
# Feedback review inbox actions (R6.5–R6.11)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Feedback review inbox listing (R6.1–R6.4)
# ---------------------------------------------------------------------------


@app.get(
    "/feedback",
    response_model=FeedbackInboxPage,
    dependencies=[Depends(require_operator)],
)
def list_feedback_endpoint(
    _operator: UserPublic = Depends(require_operator),
    settings: Settings = Depends(get_settings),
    review_status: ReviewStatus | None = Query(
        default=None, description="Filter by review status (unreviewed/reviewed/resolved)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque pagination cursor from a previous page"
    ),
    page_size: int | None = Query(
        default=None, description="Page size (clamped to the configured max)"
    ),
) -> FeedbackInboxPage:
    """Cursor-paginated inbox of negative-rating feedback, newest-first (R6.1–R6.4).

    Operator-only. Each item is joined with its query trace for full context;
    an absent/expired trace yields empty context fields rather than dropping the
    item. Rejects a tampered/invalid cursor with ``invalid_cursor``.
    """
    from rag_system.feedback import (
        FeedbackListParams,
        InvalidCursorError,
        list_feedback_inbox,
    )

    service = get_service()
    params = FeedbackListParams(
        review_status=review_status,
        page_size=page_size,
        cursor=cursor,
    )
    try:
        return list_feedback_inbox(
            service.list_feedback_reviews(),
            trace_of=lambda item: service.get_query_trace(item.trace_id),
            params=params,
            pagination_signing_key=settings.pagination_signing_key,
            page_size_limit=settings.corpus_page_size,
        )
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="invalid_cursor")


@app.post(
    "/feedback/{feedback_id}/classify",
    response_model=FeedbackReviewRecord,
    dependencies=[Depends(require_operator)],
)
def classify_feedback_endpoint(
    feedback_id: str,
    body: FeedbackClassifyRequest,
    operator: UserPublic = Depends(require_operator),
) -> FeedbackReviewRecord:
    """Classify a Feedback_Item with a Failure_Category (R6.5, R6.10).

    Operator-only. Validates the category against the six allowed values; rejects
    with ``invalid_failure_category`` otherwise. Persists category, reviewer, and
    timestamp; sets review_status to ``reviewed``, replacing any prior category.
    """
    from rag_system.feedback import InvalidFailureCategoryError

    service = get_service()
    try:
        record = service.classify_feedback(
            feedback_id, category=body.category, reviewer=operator.email
        )
    except InvalidFailureCategoryError:
        raise HTTPException(status_code=400, detail="invalid_failure_category")
    if record is None:
        raise HTTPException(status_code=404, detail="Feedback item not found.")
    return record


@app.post(
    "/feedback/{feedback_id}/promote",
    response_model=BenchmarkCase,
    dependencies=[Depends(require_operator)],
)
def promote_feedback_endpoint(
    feedback_id: str,
    _operator: UserPublic = Depends(require_operator),
) -> BenchmarkCase:
    """Promote a reviewed Feedback_Item into the Evaluation_Set (R6.6, R6.7, R6.11).

    Operator-only. Creates one Benchmark_Case from the item's question and
    expected answer. Returns ``expected_answer_required`` when no expected answer
    is present; ``already_in_evaluation_set`` when the item was already promoted.
    """
    from rag_system.feedback import (
        AlreadyInEvaluationSetError,
        ExpectedAnswerRequiredError,
    )

    service = get_service()
    try:
        case = service.promote_feedback(feedback_id)
    except ExpectedAnswerRequiredError:
        raise HTTPException(status_code=400, detail="expected_answer_required")
    except AlreadyInEvaluationSetError:
        raise HTTPException(status_code=409, detail="already_in_evaluation_set")
    if case is None:
        raise HTTPException(status_code=404, detail="Feedback item not found.")
    return case


@app.post(
    "/feedback/{feedback_id}/resolve",
    response_model=FeedbackReviewRecord,
    dependencies=[Depends(require_operator)],
)
def resolve_feedback_endpoint(
    feedback_id: str,
    _operator: UserPublic = Depends(require_operator),
) -> FeedbackReviewRecord:
    """Mark a Feedback_Item as resolved, keeping it in the inbox (R6.8).

    Operator-only. Sets review_status to ``resolved``; the item remains visible
    in the inbox and filterable by review_status.
    """
    service = get_service()
    record = service.resolve_feedback(feedback_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Feedback item not found.")
    return record


# ---------------------------------------------------------------------------
# Multi-method evaluation runs (R7)
# ---------------------------------------------------------------------------


@app.post(
    "/evaluation/runs",
    response_model=EvaluationRunDetail,
    dependencies=[Depends(require_operator)],
)
def run_evaluation_endpoint(
    _operator: UserPublic = Depends(require_operator),
) -> EvaluationRunDetail:
    """Trigger a deterministic evaluation run over the default set (R7.1–R7.6).

    Operator-only. Returns the persisted run detail. Fails with
    ``evaluation_set_invalid`` when the set has no human-reviewed case (R7.4).
    """
    from rag_system.evaluation import EvaluationSetValidationError

    service = get_service()
    try:
        return service.run_evaluation()
    except EvaluationSetValidationError as exc:
        raise HTTPException(status_code=400, detail="evaluation_set_invalid") from exc


@app.get(
    "/evaluation/runs",
    response_model=list[EvaluationRunSummary],
    dependencies=[Depends(require_operator)],
)
def list_evaluation_runs_endpoint(
    _operator: UserPublic = Depends(require_operator),
) -> list[EvaluationRunSummary]:
    """List persisted evaluation runs, newest first (R7.7). Operator-only."""
    return get_service().list_evaluation_runs()


@app.get(
    "/evaluation/runs/{run_id}",
    response_model=EvaluationRunDetail,
    dependencies=[Depends(require_operator)],
)
def get_evaluation_run_endpoint(
    run_id: str,
    _operator: UserPublic = Depends(require_operator),
) -> EvaluationRunDetail:
    """Return the full detail of an evaluation run (R7.7). Operator-only."""
    detail = get_service().get_evaluation_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="evaluation_run_not_found")
    return detail


# ---------------------------------------------------------------------------
# Corpus listing (R4)
# ---------------------------------------------------------------------------


@app.get(
    "/corpus",
    response_model=CorpusPage,
    dependencies=[Depends(get_current_user)],
)
def list_corpus_endpoint(
    user: UserPublic = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    page_size: int | None = Query(default=None, description="Page size (clamped to configured max)"),
    cursor: str | None = Query(default=None, description="Opaque pagination cursor from a previous page"),
    sort_field: str | None = Query(default=None, description="Sort field: name, owner, or date"),
    sort_direction: str | None = Query(default=None, description="Sort direction: asc or desc"),
    status_filter: DocumentStatus | None = Query(default=None, alias="status", description="Filter by document status"),
    owner_filter: str | None = Query(default=None, alias="owner", description="Filter by owner"),
    date_from: str | None = Query(default=None, description="Inclusive lower bound on document date (ISO-8601)"),
    date_to: str | None = Query(default=None, description="Inclusive upper bound on document date (ISO-8601)"),
    active_version: str | None = Query(default=None, description="Filter by active version"),
    search: str | None = Query(default=None, description="Case-insensitive metadata search (1-200 chars)"),
) -> CorpusPage:
    """Cursor-paginated corpus listing with sort/filter/search (R4.1–R4.14).

    Available to all authenticated users. Non-operators see only documents whose
    ``owner`` equals their authenticated identity; operators see the full corpus.
    """
    from rag_system.auth.dependencies import resolve_is_operator
    from rag_system.corpus import (
        CorpusListParams,
        InvalidCursorError,
        SearchTermTooLongError,
        SortDirection,
        SortField,
        list_corpus,
    )

    is_operator = resolve_is_operator(user, settings)

    # Build the listing params from query string values.
    resolved_sort_field = SortField.name
    if sort_field is not None:
        try:
            resolved_sort_field = SortField(sort_field)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid sort_field: '{sort_field}'. Must be one of: name, owner, date.",
            )

    resolved_sort_direction = SortDirection.asc
    if sort_direction is not None:
        try:
            resolved_sort_direction = SortDirection(sort_direction)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid sort_direction: '{sort_direction}'. Must be one of: asc, desc.",
            )

    params = CorpusListParams(
        sort_field=resolved_sort_field,
        sort_direction=resolved_sort_direction,
        status=status_filter,
        owner=owner_filter,
        active_version=active_version,
        date_from=date_from,
        date_to=date_to,
        search=search,
        page_size=page_size,
        cursor=cursor,
    )

    # Fetch all documents from the backend to pass to the listing service.
    all_documents = get_service().list_documents()

    try:
        return list_corpus(
            all_documents,
            viewer_identity=user.email,
            is_operator=is_operator,
            params=params,
            pagination_signing_key=settings.pagination_signing_key,
            corpus_page_size=settings.corpus_page_size,
        )
    except InvalidCursorError:
        raise HTTPException(
            status_code=400,
            detail="invalid_cursor",
        )
    except SearchTermTooLongError:
        raise HTTPException(
            status_code=400,
            detail="search_term_too_long",
        )


# ---------------------------------------------------------------------------
# Versioned AI configuration (R9)
# ---------------------------------------------------------------------------


def _get_ai_config_store():
    """Lazily construct the AIConfigurationStore for ai-config endpoints."""
    from rag_system.ai_config import AIConfigurationStore

    store = _get_artifact_store()
    return AIConfigurationStore(store)


@app.put(
    "/ai-config/{config_id}",
    response_model=AIConfigurationVersion,
    status_code=201,
    dependencies=[Depends(require_operator)],
)
def create_ai_config_version(
    config_id: str,
    body: AIConfigCreateRequest,
) -> AIConfigurationVersion:
    """Create a new immutable AI configuration version (R9.3, R9.4).

    Operator-only. Validates the 1–500 char change description; rejects with
    ``change_description_required`` otherwise (no version created, active
    unchanged).
    """
    from rag_system.ai_config import ChangeDescriptionRequiredError

    config_store = _get_ai_config_store()
    try:
        version = config_store.create_version(
            config_id,
            prompt=body.prompt,
            model=body.model,
            router_threshold=body.router_threshold,
            change_description=body.change_description,
            output_schema=body.output_schema,
            retrieval_settings=body.retrieval_settings,
            reranker_config=body.reranker_config,
        )
    except ChangeDescriptionRequiredError:
        raise HTTPException(status_code=400, detail="change_description_required")
    return version


@app.get(
    "/ai-config/{config_id}/history",
    response_model=list[AIConfigurationVersion],
    dependencies=[Depends(require_operator)],
)
def get_ai_config_history(config_id: str) -> list[AIConfigurationVersion]:
    """Return AI configuration versions reverse-chronologically (R9.5, R9.6).

    Operator-only. Returns an empty list when no versions exist.
    """
    config_store = _get_ai_config_store()
    return config_store.get_history(config_id)


@app.post(
    "/ai-config/{config_id}/rollback",
    response_model=ActivationEvent,
    status_code=200,
    dependencies=[Depends(require_operator)],
)
def rollback_ai_config(
    config_id: str,
    body: AIConfigRollbackRequest,
    operator: UserPublic = Depends(require_operator),
) -> ActivationEvent:
    """Rollback to an existing AI configuration version (R9.8, R9.9, R9.10).

    Operator-only. Sets the target version active and records an ActivationEvent.
    Unknown version → 404 ``configuration_version_not_found`` with active unchanged.
    """
    from rag_system.ai_config import ConfigurationVersionNotFoundError

    config_store = _get_ai_config_store()
    try:
        event = config_store.rollback(
            config_id,
            version_id=body.version_id,
            operator=operator.email,
            reason=body.reason,
        )
    except ConfigurationVersionNotFoundError:
        raise HTTPException(status_code=404, detail="configuration_version_not_found")
    return event


@app.post(
    "/ai-config/{config_id}/versions/{version_id}/approve",
    response_model=AIConfigurationVersion,
    dependencies=[Depends(require_operator)],
)
def approve_ai_config_version(
    config_id: str,
    version_id: str,
    operator: UserPublic = Depends(require_operator),
) -> AIConfigurationVersion:
    """Approve an AI configuration version (R8.3, R9.7).

    Operator-only. Sets approved=True, records the approver identity and
    approval timestamp. Approval does NOT mutate the version's governed settings
    (prompt, model, output_schema, router_threshold, retrieval_settings,
    reranker_config). Unknown version → 404 ``configuration_version_not_found``.
    """
    from rag_system.ai_config import ConfigurationVersionNotFoundError

    config_store = _get_ai_config_store()
    try:
        approved_version = config_store.approve_version(
            config_id,
            version_id=version_id,
            approver=operator.email,
        )
    except ConfigurationVersionNotFoundError:
        raise HTTPException(status_code=404, detail="configuration_version_not_found")
    return approved_version


# ---------------------------------------------------------------------------
# Trace Investigator (R10)
# ---------------------------------------------------------------------------


def _get_trace_investigator():
    """Lazily construct the TraceInvestigator for the diagnose endpoint."""
    from rag_system.trace_investigator import TraceInvestigator

    settings = get_settings()
    service = get_service()

    def _resolve_trace(trace_id: str):
        return service.get_query_trace(trace_id)

    return TraceInvestigator(settings, trace_resolver=_resolve_trace)


@app.post(
    "/traces/{trace_id}/diagnose",
    response_model=TraceDiagnosis,
    dependencies=[Depends(require_operator)],
)
def diagnose_trace(trace_id: str) -> TraceDiagnosis:
    """Diagnose a recorded query trace (R10.1, R10.6).

    Operator-only. Loads the enriched query trace, analyzes route/retrieval/rerank/
    generation outcome, and returns read-only recommendations. No mutations are
    applied to the trace or any other state (R10.7).

    Returns 404 ``trace_not_found`` when the trace is not recorded (R10.2).
    """
    from rag_system.trace_investigator import TraceNotFoundError

    investigator = _get_trace_investigator()
    try:
        diagnosis = investigator.diagnose(trace_id)
    except TraceNotFoundError:
        raise HTTPException(status_code=404, detail="trace_not_found")
    return diagnosis


# ---------------------------------------------------------------------------
# Knowledge Gap Map (R11)
# ---------------------------------------------------------------------------


@app.post(
    "/knowledge-gap-map",
    response_model=KnowledgeGapMap,
    dependencies=[Depends(require_operator)],
)
def generate_knowledge_gap_map_endpoint(
    settings: Settings = Depends(get_settings),
) -> KnowledgeGapMap:
    """Generate the Knowledge Gap Map from eligible query outcomes (R11.1–R11.6).

    Operator-only. Scans stored query traces and feedback, clusters eligible
    outcomes, and returns the gap map with topics and recommendations.
    On generation failure returns ``knowledge_gap_generation_failed`` (R11.5).
    """
    from rag_system.knowledge_gap import (
        KnowledgeGapGenerationError,
        generate_knowledge_gap_map,
        select_eligible_outcomes,
    )

    service = get_service()
    store = service.artifact_store

    # Gather all query traces from storage.
    traces: list[QueryTraceRecord] = []
    for key in store.list_query_trace_keys():
        payload = store.get_json(key)
        if payload is not None:
            traces.append(QueryTraceRecord.model_validate(payload))

    # Gather all feedback records.
    feedback_items: list[FeedbackReviewRecord] = []
    for key in store.list_feedback_record_keys():
        payload = store.get_json(key)
        if payload is not None:
            feedback_items.append(FeedbackReviewRecord.model_validate(payload))

    # Select eligible outcomes.
    outcomes = select_eligible_outcomes(
        traces,
        feedback_items,
        confidence_threshold=settings.route_min_confidence,
    )

    # Generate the gap map.
    try:
        gap_map = generate_knowledge_gap_map(
            outcomes,
            embed_question=_get_embed_question(),
            label_cluster=_get_label_cluster(),
            max_topics=settings.knowledge_gap_max_topics,
            min_eligible_outcomes=settings.knowledge_gap_min_eligible_outcomes,
        )
    except KnowledgeGapGenerationError:
        raise HTTPException(
            status_code=500,
            detail="knowledge_gap_generation_failed",
        )

    return gap_map


def _get_embed_question():
    """Return the embedding function for knowledge-gap clustering."""
    service = get_service()
    return lambda question: service.embedder.embed_query(question)


def _get_label_cluster():
    """Return the cluster labeling function for knowledge-gap topic labels."""
    from rag_system.llm import build_text_llm

    settings = get_settings()
    # Use gemini-3.5-flash for cluster labeling, overriding the default model.
    label_settings = settings.model_copy(
        update={
            "gemini_model_id": "gemini-3.5-flash",
            "gemini_read_timeout_s": 30,
        }
    )
    llm = build_text_llm(label_settings)

    def label(questions: list[str]) -> str:
        prompt = (
            "Summarize the following questions into a single short topic label "
            "(max 5 words):\n" + "\n".join(f"- {q}" for q in questions)
        )
        return llm(prompt)

    return label
