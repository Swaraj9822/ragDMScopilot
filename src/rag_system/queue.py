import json
from typing import Any

from pydantic import BaseModel

from rag_system.config import Settings
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)


class IngestionJob(BaseModel):
    document_id: str
    version: str
    filename: str
    s3_uri: str
    trace_id: str | None = None


class ReceivedIngestionJob(BaseModel):
    job: IngestionJob
    receipt_handle: str
    message_id: str | None = None
    receive_count: int = 0


class SqsIngestionQueue:
    def __init__(self, settings: Settings):
        self._queue_url = settings.ingestion_queue_url
        self._poll_seconds = settings.ingestion_poll_seconds
        self._max_messages = settings.ingestion_max_messages
        self._client = settings.boto3_session().client("sqs")
        logger.info("SqsIngestionQueue initialised")

    @retry_on_transient()
    def enqueue(self, job: IngestionJob) -> str:
        response = self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=job.model_dump_json(),
        )
        message_id = response.get("MessageId", "")
        metrics.increment("rag_ingestion_jobs_enqueued_total")
        logger.info(
            "Enqueued ingestion job",
            extra={"document_id": job.document_id, "version": job.version},
        )
        return message_id

    @retry_on_transient()
    def receive(self) -> list[ReceivedIngestionJob]:
        response = self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=max(1, min(10, self._max_messages)),
            WaitTimeSeconds=max(0, min(20, self._poll_seconds)),
            AttributeNames=["ApproximateReceiveCount"],
        )
        jobs: list[ReceivedIngestionJob] = []
        for message in response.get("Messages", []):
            try:
                jobs.append(_parse_message(message))
            except Exception as e:
                logger.error("Failed to parse message: %s", e, exc_info=True)
                try:
                    self._client.delete_message(
                        QueueUrl=self._queue_url,
                        ReceiptHandle=message["ReceiptHandle"],
                    )
                except Exception as del_e:
                    logger.error("Failed to delete poison pill message: %s", del_e, exc_info=True)
                continue
        metrics.observe("rag_ingestion_jobs_received", len(jobs))
        return jobs

    @retry_on_transient()
    def delete(self, received: ReceivedIngestionJob) -> None:
        self._client.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=received.receipt_handle,
        )
        metrics.increment("rag_ingestion_jobs_deleted_total")
        logger.info(
            "Deleted ingestion job message",
            extra={
                "document_id": received.job.document_id,
                "version": received.job.version,
            },
        )

    @retry_on_transient()
    def extend_visibility(self, receipt_handle: str, timeout_seconds: int) -> None:
        """Extends the SQS visibility timeout for long-running jobs."""
        self._client.change_message_visibility(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=timeout_seconds,
        )


def _parse_message(message: dict[str, Any]) -> ReceivedIngestionJob:
    payload = json.loads(message["Body"])
    attributes = message.get("Attributes") or {}
    receive_count = int(attributes.get("ApproximateReceiveCount", "1"))
    return ReceivedIngestionJob(
        job=IngestionJob.model_validate(payload),
        receipt_handle=message["ReceiptHandle"],
        message_id=message.get("MessageId"),
        receive_count=receive_count,
    )
