import asyncio
import time
import uuid
import re
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
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
    CircuitOpenError,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern lifecycle hook — replaces deprecated on_event('startup'/'shutdown')."""
    metrics.start_cloudwatch_flusher(get_settings().boto3_session())
    logger.info("Application startup complete")
    yield
    metrics.stop_cloudwatch_flusher()
    logger.info("Application shutdown complete")


app = FastAPI(title="Production RAG", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_allowed_origins.split(",") if o.strip()],
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

    # Normalize path for metrics to avoid high cardinality
    norm_path = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "{id}",
        request.url.path,
        flags=re.IGNORECASE,
    )

    logger.info(
        "→ %s %s",
        request.method,
        request.url.path,
        extra={"method": request.method, "path": norm_path},
    )

    # ---- Determine per-route timeout ----
    settings = get_settings()
    timeout_s: int | None = None
    if request.method == "POST":
        if request.url.path == "/query":
            timeout_s = settings.request_timeout_query_s
        elif request.url.path == "/copilot/query":
            timeout_s = settings.request_timeout_copilot_s
        elif request.url.path == "/ask":
            timeout_s = settings.request_timeout_ask_s
    # 0 means disabled
    if timeout_s == 0:
        timeout_s = None

    try:
        if timeout_s:
            response = await asyncio.wait_for(call_next(request), timeout=timeout_s)
        else:
            response = await call_next(request)
    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        labels = {
            "method": request.method,
            "path": norm_path,
            "status_code": "504",
        }
        metrics.increment("rag_http_requests_total", labels)
        metrics.observe("rag_http_request_duration_ms", elapsed_ms, labels)
        metrics.increment("rag_request_timeout_total", {"path": norm_path})
        logger.error(
            "← %s %s 504 TIMEOUT (%.0fms, limit=%ds)",
            request.method,
            request.url.path,
            elapsed_ms,
            timeout_s,
            extra={
                "method": request.method,
                "path": norm_path,
                "status_code": 504,
                "duration_ms": elapsed_ms,
                "timeout_limit_s": timeout_s,
            },
        )
        reset_trace_id(token)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=504,
            content={
                "detail": f"Request timed out after {timeout_s}s. "
                "The operation is taking longer than expected. "
                "Please try a simpler query or try again later.",
                "trace_id": trace_id,
                "timeout_seconds": timeout_s,
            },
            headers={"X-Trace-Id": trace_id},
        )
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        labels = {
            "method": request.method,
            "path": norm_path,
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
                "path": norm_path,
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
        "path": norm_path,
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
            "path": norm_path,
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


@app.get("/ready")
async def readiness() -> dict[str, object]:
    """Readiness check — verifies critical dependency connectivity."""
    import asyncio as _asyncio

    settings = get_settings()
    timeout = settings.readiness_probe_timeout_s
    results: dict[str, str] = {}

    async def probe_s3() -> str:
        try:
            client = settings.boto3_session().client("s3")
            await run_in_threadpool(client.head_bucket, Bucket=settings.s3_bucket)
            return "ok"
        except Exception as e:
            return f"error: {e}"

    async def probe_pinecone() -> str:
        try:
            index = get_service().index
            await run_in_threadpool(index._index.describe_index_stats)
            return "ok"
        except Exception as e:
            return f"error: {e}"

    async def probe_bedrock() -> str:
        try:
            # Verify client construction works (no model call to avoid cost)
            settings.boto3_session().client(
                "bedrock-runtime", config=settings.bedrock_botocore_config()
            )
            return "ok"
        except Exception as e:
            return f"error: {e}"

    async def probe_postgres() -> str:
        if not settings.copilot_db_host:
            return "skipped"
        try:
            executor = get_copilot_service().executor
            await run_in_threadpool(executor._get_pool().connection().__enter__)
            # Light query
            pool = executor._get_pool()
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return "ok"
        except Exception as e:
            return f"error: {e}"

    probes = {
        "s3": probe_s3,
        "pinecone": probe_pinecone,
        "bedrock": probe_bedrock,
        "postgres": probe_postgres,
    }

    async def run_probe(name: str, fn):
        try:
            result = await _asyncio.wait_for(fn(), timeout=timeout)
        except _asyncio.TimeoutError:
            result = f"error: timeout ({timeout}s)"
        except Exception as e:
            result = f"error: {e}"
        results[name] = result

    await _asyncio.gather(*(run_probe(name, fn) for name, fn in probes.items()))

    all_ok = all(v == "ok" or v == "skipped" for v in results.values())
    status_code = 200 if all_ok else 503
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if all_ok else "degraded", "dependencies": results},
    )


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
    return await run_in_threadpool(service.queue_pdf, file.filename or "document", content)


@app.put(
    "/documents/{document_id}",
    response_model=DocumentRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_document(
    document_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
) -> DocumentRecord:
    content = await _read_document_upload(request, file)

    record = await run_in_threadpool(
        get_service().update_document,
        str(document_id),
        file.filename or "document",
        content,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@app.delete("/documents/{document_id}", response_model=DocumentRecord)
async def delete_document(document_id: uuid.UUID) -> DocumentRecord:
    record = await run_in_threadpool(get_service().delete_document, str(document_id))
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


async def _read_document_upload(request: Request, file: UploadFile) -> bytes:
    filename = file.filename or ""
    ext = filename[filename.rfind(".") :].lower() if "." in filename else ""
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
def get_document(document_id: uuid.UUID) -> DocumentRecord:
    record = get_service().get_document(str(document_id))
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@app.post("/ask", response_model=UnifiedQueryResponse)
async def ask(request: UnifiedQueryRequest) -> UnifiedQueryResponse:
    """Unified endpoint — auto-routes to RAG, database copilot, or both."""
    logger.info(
        "Unified query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question), "user_id": request.user_id},
    )
    try:
        return await run_in_threadpool(get_router().query, request)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CircuitOpenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest) -> QueryResponse:
    logger.info(
        "Query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question)},
    )
    try:
        return await run_in_threadpool(get_service().query, request)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CircuitOpenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/copilot/query", response_model=CopilotQueryResponse)
async def copilot_query(request: CopilotQueryRequest) -> CopilotQueryResponse:
    logger.info(
        "Copilot query received (%d chars)",
        len(request.question),
        extra={"query_len": len(request.question), "user_id": request.user_id},
    )
    try:
        return await run_in_threadpool(get_copilot_service().query, request)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CircuitOpenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
