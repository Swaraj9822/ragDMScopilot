"""Unit tests for the Pub/Sub-backed ingestion queue.

The queue is constructed via ``object.__new__`` with fake publisher/subscriber
clients, so no real Pub/Sub client or credentials are needed. These cover the
migration-critical behaviours: publishing the job payload as JSON bytes, parsing
a pulled message into a ``ReceivedIngestionJob`` (ack_id + job), and
acknowledging a handled message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from rag_system.queue import IngestionJob, PubSubIngestionQueue


class _FakeFuture:
    def __init__(self, message_id: str) -> None:
        self._id = message_id

    def result(self):
        return self._id


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[SimpleNamespace] = []

    def publish(self, topic_path, data):
        self.published.append(SimpleNamespace(topic_path=topic_path, data=data))
        return _FakeFuture("mid-1")


class _FakeSubscriber:
    def __init__(self, messages=None) -> None:
        self._messages = messages or []
        self.acked: list[list[str]] = []
        self.pull_requests: list[dict] = []

    def pull(self, request, timeout=None):
        self.pull_requests.append(request)
        return SimpleNamespace(received_messages=self._messages)

    def acknowledge(self, request):
        self.acked.append(request["ack_ids"])


def _queue(subscriber: _FakeSubscriber | None = None) -> PubSubIngestionQueue:
    q = object.__new__(PubSubIngestionQueue)
    q._project_id = "p"
    q._poll_seconds = 5
    q._max_messages = 10
    q._publisher = _FakePublisher()
    q._subscriber = subscriber or _FakeSubscriber()
    q._topic_path = "projects/p/topics/rag-ingestion"
    q._subscription_path = "projects/p/subscriptions/rag-ingestion-sub"
    return q


def _job() -> IngestionJob:
    return IngestionJob(
        document_id="doc-1",
        version="v1",
        filename="source.pdf",
        s3_uri="gs://b/raw/doc-1/v1/source.pdf",
    )


def test_enqueue_publishes_job_json_bytes_and_returns_message_id() -> None:
    q = _queue()
    message_id = q.enqueue(_job())

    assert message_id == "mid-1"
    published = q._publisher.published[-1]
    assert published.topic_path == "projects/p/topics/rag-ingestion"
    payload = json.loads(published.data.decode("utf-8"))
    assert payload["document_id"] == "doc-1"
    assert payload["s3_uri"] == "gs://b/raw/doc-1/v1/source.pdf"


def test_receive_parses_ack_id_and_job() -> None:
    message = SimpleNamespace(
        ack_id="ack-123",
        message=SimpleNamespace(
            data=_job().model_dump_json().encode("utf-8"),
            message_id="m-1",
        ),
    )
    subscriber = _FakeSubscriber([message])
    q = _queue(subscriber)

    received = q.receive()

    assert len(received) == 1
    assert received[0].ack_id == "ack-123"
    assert received[0].message_id == "m-1"
    assert received[0].job.document_id == "doc-1"
    # The pull is bounded by max_messages (clamped to [1, 10]).
    assert subscriber.pull_requests[0]["max_messages"] == 10


def test_receive_empty_subscription_returns_empty_list() -> None:
    q = _queue(_FakeSubscriber([]))
    assert q.receive() == []


def test_delete_acknowledges_the_message() -> None:
    subscriber = _FakeSubscriber()
    q = _queue(subscriber)
    received = SimpleNamespace(
        ack_id="ack-xyz",
        job=SimpleNamespace(document_id="doc-1", version="v1"),
    )

    q.delete(received)

    assert subscriber.acked == [["ack-xyz"]]
