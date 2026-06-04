import time
import uuid
from functools import lru_cache

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from rag_system.config import get_settings
from rag_system.copilot import DatabaseCopilotService, SqlValidationError
from rag_system.models import (
    CopilotQueryRequest,
    CopilotQueryResponse,
    DocumentRecord,
    QueryRequest,
    QueryResponse,
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
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Uploaded file is too large. Maximum size is {max_upload_bytes} bytes.",
            )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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
