import asyncio
import uuid
import signal
from pathlib import Path

from rag_system.config import get_settings
from rag_system.observability import (
    get_logger,
    metrics,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)
from rag_system.queue import ReceivedIngestionJob, SqsIngestionQueue
from rag_system.service import RagService

logger = get_logger(__name__)


class IngestionWorker:
    def __init__(
        self,
        service: RagService,
        queue: SqsIngestionQueue,
    ) -> None:
        self._service = service
        self._queue = queue
        self._shutting_down = False
        self._consecutive_failures = 0
        self._health_file = Path("/tmp/worker-healthy")
        self._max_receive_count = service._settings.ingestion_max_receive_count

    def _signal_handler(self) -> None:
        logger.info("Received shutdown signal, initiating graceful shutdown")
        self._shutting_down = True

    async def process_once(self) -> int:
        messages = self._queue.receive()
        for message in messages:
            if self._shutting_down:
                logger.info("Worker shutting down, ignoring remaining messages in batch")
                break
            await self._process_message(message)

        # Touch health file on successful poll/process
        self._health_file.parent.mkdir(parents=True, exist_ok=True)
        self._health_file.touch()

        return len(messages)

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._signal_handler)
        except NotImplementedError:
            pass  # Windows

        metrics.start_cloudwatch_flusher(self._service._settings.boto3_session())
        logger.info("Ingestion worker started")

        try:
            while not self._shutting_down:
                try:
                    await self.process_once()
                    self._consecutive_failures = 0
                except Exception as e:
                    self._consecutive_failures += 1
                    backoff = min(60.0, (2**self._consecutive_failures))
                    logger.error(
                        "Unhandled exception in worker loop: %s. Backing off for %.1fs",
                        e,
                        backoff,
                        exc_info=True,
                    )
                    await asyncio.sleep(backoff)
        finally:
            metrics.stop_cloudwatch_flusher()
            logger.info("Ingestion worker shutdown gracefully")
        if self._health_file.exists():
            self._health_file.unlink(missing_ok=True)

    async def _process_message(self, message: ReceivedIngestionJob) -> None:
        job = message.job
        trace_id = job.trace_id or str(uuid.uuid4())
        token = set_trace_id(trace_id)

        async def keep_alive() -> None:
            try:
                while True:
                    await asyncio.sleep(15.0)  # extend before default 30s timeout
                    # change_message_visibility is synchronous in boto3, but we just call it
                    try:
                        self._queue.extend_visibility(message.receipt_handle, 30)
                    except Exception as e:
                        logger.warning("Failed to extend message visibility: %s", e)
            except asyncio.CancelledError:
                pass

        keep_alive_task = asyncio.create_task(keep_alive())

        try:
            logger.info(
                "Processing ingestion job",
                extra={"document_id": job.document_id, "version": job.version},
            )
            await self._service.process_document_job(job)
            keep_alive_task.cancel()
            self._queue.delete(message)
            metrics.increment("rag_ingestion_jobs_completed_total")
        except Exception as exc:
            keep_alive_task.cancel()
            if message.receive_count >= self._max_receive_count:
                # Abandon: exceeded retry limit
                logger.error(
                    "Ingestion job exceeded max receive count (%d/%d); abandoning",
                    message.receive_count,
                    self._max_receive_count,
                    extra={"document_id": job.document_id, "version": job.version},
                )
                self._service.mark_failed(
                    job.document_id,
                    f"Abandoned after {message.receive_count} attempts. Last error: {exc}",
                )
                self._queue.delete(message)
                metrics.increment("rag_ingestion_jobs_abandoned_total")
            else:
                metrics.increment("rag_ingestion_jobs_failed_total")
                logger.error(
                    "Ingestion job failed (attempt %d/%d); leaving on queue for retry",
                    message.receive_count,
                    self._max_receive_count,
                    extra={"document_id": job.document_id, "version": job.version},
                    exc_info=True,
                )
        finally:
            reset_trace_id(token)


async def amain() -> None:
    setup_logging()
    settings = get_settings()
    service = RagService(settings)
    await IngestionWorker(service, service.queue).run_forever()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
