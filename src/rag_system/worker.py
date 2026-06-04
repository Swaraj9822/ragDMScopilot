import asyncio
import uuid

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

    async def process_once(self) -> int:
        messages = self._queue.receive()
        for message in messages:
            await self._process_message(message)
        return len(messages)

    async def run_forever(self) -> None:
        logger.info("Ingestion worker started")
        while True:
            await self.process_once()

    async def _process_message(self, message: ReceivedIngestionJob) -> None:
        job = message.job
        trace_id = job.trace_id or str(uuid.uuid4())
        token = set_trace_id(trace_id)
        try:
            logger.info(
                "Processing ingestion job",
                extra={"document_id": job.document_id, "version": job.version},
            )
            await self._service.process_document_job(job)
            self._queue.delete(message)
            metrics.increment("rag_ingestion_jobs_completed_total")
        except Exception:
            metrics.increment("rag_ingestion_jobs_failed_total")
            logger.error(
                "Ingestion job failed; leaving message on queue for retry",
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
