import asyncio
import logging
import uuid

from rag_system.config import Settings, get_settings
from rag_system.observability import (
    get_logger,
    metrics,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)
from rag_system.observability_tracing import get_span_recorder
from rag_system.queue import ReceivedIngestionJob, SqsIngestionQueue
from rag_system.service import DocumentDeletedError, RagService

logger = get_logger(__name__)

#: Seconds to wait after a failed poll cycle before retrying, so a transient
#: queue/network error backs off instead of spinning in a tight loop.
ERROR_BACKOFF_SECONDS = 5.0

#: Seconds to pause when a poll returned no messages, guarding against a hot
#: loop if the queue is configured with a short/zero long-poll wait.
IDLE_SLEEP_SECONDS = 1.0


class IngestionWorker:
    def __init__(
        self,
        service: RagService,
        queue: SqsIngestionQueue,
        max_concurrency: int | None = None,
    ) -> None:
        self._service = service
        self._queue = queue
        # How many messages from a single poll to process concurrently. Falls
        # back to the service's configured value, then to 1 (sequential) so test
        # doubles and minimal wiring behave exactly as before.
        if max_concurrency is None:
            max_concurrency = getattr(
                getattr(service, "_settings", None), "ingestion_max_concurrency", 1
            )
        self._max_concurrency = max(1, max_concurrency)

    async def process_once(self) -> int:
        messages = self._queue.receive()
        if not messages:
            return 0
        if self._max_concurrency <= 1 or len(messages) == 1:
            for message in messages:
                await self._process_message(message)
            return len(messages)

        # Drain the batch concurrently under a bounded semaphore so a burst of
        # uploads is not processed strictly one-at-a-time. Each message owns its
        # own trace/status handling and failures are contained per-message
        # inside _process_message, so gather never aborts the whole batch.
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _guarded(message: ReceivedIngestionJob) -> None:
            async with semaphore:
                await self._process_message(message)

        await asyncio.gather(*(_guarded(message) for message in messages))
        return len(messages)

    async def run_forever(self) -> None:
        logger.info("Ingestion worker started")
        while True:
            try:
                processed = await self.process_once()
            except Exception:
                # A poll/receive failure must never kill the worker loop — log it
                # and back off, then keep polling. Previously an exception here
                # propagated out of run_forever and silently stopped ingestion.
                logger.exception(
                    "Ingestion poll cycle failed; backing off before retry"
                )
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                continue
            if processed == 0:
                await asyncio.sleep(IDLE_SLEEP_SECONDS)

    async def _process_message(self, message: ReceivedIngestionJob) -> None:
        job = message.job
        # R2.9: adopt the payload trace_id when present; R2.10: generate a new
        # one when absent. We set the resolved id on the logging context here,
        # then pass job.trace_id (possibly None) to start_trace. When it is None
        # the recorder falls back to the active context trace id (the value we
        # just set), so logs and the Root_Span share the same id while the
        # sampler still applies the configured rate (has_trace_header=False).
        # A propagated, header-origin trace_id is force-sampled (R10.7).
        trace_id = job.trace_id or str(uuid.uuid4())
        token = set_trace_id(trace_id)
        try:
            logger.info(
                "Processing ingestion job",
                extra={"document_id": job.document_id, "version": job.version},
            )
            recorder = get_span_recorder()
            # R12.1: open a Root_Span representing this ingestion job.
            # R2.9/R2.10: adopt payload trace_id or fall back to the context id.
            with recorder.start_trace(
                trace_id=job.trace_id, route="ingestion", is_root_http=False
            ):
                await self._service.process_document_job(job)
            self._queue.delete(message)
            metrics.increment("rag_ingestion_jobs_completed_total")
        except DocumentDeletedError:
            # The document was deleted before this job ran (`deleted` is a
            # terminal state), so the job can never succeed. Treat it as a
            # successful no-op: delete the message so it does not redeliver
            # until maxReceiveCount and pile up in the DLQ as noise.
            self._queue.delete(message)
            metrics.increment("rag_ingestion_jobs_skipped_deleted_total")
            logger.info(
                "Ingestion job skipped: document already deleted",
                extra={"document_id": job.document_id, "version": job.version},
            )
        except Exception:
            metrics.increment("rag_ingestion_jobs_failed_total")
            logger.error(
                "Ingestion job failed; leaving message on queue for retry",
                extra={"document_id": job.document_id, "version": job.version},
                exc_info=True,
            )
        finally:
            reset_trace_id(token)


def _start_worker_observability(settings: Settings) -> None:
    """Wire trace/log persistence in the worker process (mirrors the API).

    The ingestion worker emits a Root_Span per job and one child span per stage
    (parsing, chunking, embedding, indexing), but those spans — and the worker's
    structured logs — are only persisted if the background flush workers and the
    log handler run in *this* process. The API process wires its own; without
    this, ingestion traces/logs never reach the store even though they are
    captured. Failures here must not stop ingestion, so everything is best-effort.
    """
    if not settings.tracing_enabled:
        return

    # Import lazily to keep the worker independent of the FastAPI app module.
    from rag_system.observability_tracing import schema
    from rag_system.observability_tracing.buffers import BoundedLogBuffer
    from rag_system.observability_tracing.flush_workers import (
        LogFlushWorker,
        TraceFlushWorker,
    )
    from rag_system.observability_tracing.log_handler import TracePersistingLogHandler
    from rag_system.observability_tracing.log_store import PostgresLogStore
    from rag_system.observability_tracing.trace_store import PostgresTraceStore

    try:
        schema.apply_schema(settings)
    except Exception:  # noqa: BLE001 - never let observability setup stop ingestion
        logger.exception(
            "Failed to apply observability schema in worker; ingestion traces/logs "
            "will not persist until the database is reachable. Ingestion itself is "
            "unaffected."
        )
        return

    try:
        recorder = get_span_recorder()

        log_buffer = BoundedLogBuffer(
            capacity=settings.log_buffer_capacity, metrics=metrics
        )
        logging.getLogger().addHandler(TracePersistingLogHandler(log_buffer))

        TraceFlushWorker(
            span_buffer=recorder._span_buffer,
            trace_store=PostgresTraceStore(settings),
        ).start()
        LogFlushWorker(
            log_buffer=log_buffer,
            log_store=PostgresLogStore(settings),
        ).start()
        logger.info("Worker observability started (trace + log flush workers)")
    except Exception:  # noqa: BLE001 - best-effort; ingestion must still run
        logger.exception("Failed to start worker observability; continuing without it")


async def amain() -> None:
    setup_logging()
    settings = get_settings()
    _start_worker_observability(settings)
    service = RagService(settings)
    await IngestionWorker(service, service.queue).run_forever()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
