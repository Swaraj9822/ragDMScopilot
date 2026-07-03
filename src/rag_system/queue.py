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
    # Opaque artifact URI for the uploaded source (now a gs:// URI). Kept named
    # ``s3_uri`` for backward compatibility with persisted records and the API
    # contract; the value is informational and never parsed to fetch content.
    s3_uri: str
    trace_id: str | None = None


class ReceivedIngestionJob(BaseModel):
    job: IngestionJob
    # Pub/Sub acknowledgement id, used to ack (delete) the message once handled.
    ack_id: str
    message_id: str | None = None


class PubSubIngestionQueue:
    """Ingestion queue backed by Google Cloud Pub/Sub.

    Publishes ingestion jobs to a topic and pulls them from a subscription.
    Pub/Sub's *ack deadline* plays the role SQS's *visibility timeout* did: a
    pulled-but-unacked message is redelivered after the deadline, so the
    subscription's ack deadline must comfortably exceed worst-case ingestion
    time. A handled message is acknowledged via :meth:`delete`.
    """

    def __init__(self, settings: Settings):
        if not settings.gcp_project_id:
            raise RuntimeError(
                "GCP_PROJECT_ID must be set to use the Pub/Sub ingestion queue."
            )
        if not settings.pubsub_topic_id or not settings.pubsub_subscription_id:
            raise RuntimeError(
                "RAG_PUBSUB_TOPIC_ID and RAG_PUBSUB_SUBSCRIPTION_ID must be set "
                "to use the Pub/Sub ingestion queue."
            )
        self._project_id = settings.gcp_project_id
        self._poll_seconds = settings.ingestion_poll_seconds
        self._max_messages = settings.ingestion_max_messages
        self._publisher = settings.pubsub_publisher()
        self._subscriber = settings.pubsub_subscriber()
        self._topic_path = self._publisher.topic_path(
            self._project_id, settings.pubsub_topic_id
        )
        self._subscription_path = self._subscriber.subscription_path(
            self._project_id, settings.pubsub_subscription_id
        )
        logger.info("PubSubIngestionQueue initialised")

    @retry_on_transient()
    def enqueue(self, job: IngestionJob) -> str:
        future = self._publisher.publish(
            self._topic_path, data=job.model_dump_json().encode("utf-8")
        )
        message_id = future.result()
        metrics.increment("rag_ingestion_jobs_enqueued_total")
        logger.info(
            "Enqueued ingestion job",
            extra={"document_id": job.document_id, "version": job.version},
        )
        return message_id

    @retry_on_transient()
    def receive(self) -> list[ReceivedIngestionJob]:
        response = self._subscriber.pull(
            request={
                "subscription": self._subscription_path,
                "max_messages": max(1, min(10, self._max_messages)),
            },
            # Bound the synchronous pull so an empty subscription returns instead
            # of blocking the worker loop indefinitely.
            timeout=max(1, min(20, self._poll_seconds)),
        )
        jobs: list[ReceivedIngestionJob] = []
        for received in response.received_messages:
            jobs.append(_parse_message(received))
        metrics.observe("rag_ingestion_jobs_received", len(jobs))
        return jobs

    @retry_on_transient()
    def delete(self, received: ReceivedIngestionJob) -> None:
        self._subscriber.acknowledge(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [received.ack_id],
            }
        )
        metrics.increment("rag_ingestion_jobs_deleted_total")
        logger.info(
            "Acknowledged ingestion job message",
            extra={
                "document_id": received.job.document_id,
                "version": received.job.version,
            },
        )


def _parse_message(received: Any) -> ReceivedIngestionJob:
    payload = json.loads(received.message.data.decode("utf-8"))
    return ReceivedIngestionJob(
        job=IngestionJob.model_validate(payload),
        ack_id=received.ack_id,
        message_id=received.message.message_id or None,
    )
