import asyncio
import threading
import time
from types import SimpleNamespace

from rag_system.models import Chunk, DocumentRecord, DocumentStatus, EmbeddedChunk, ParsedDocument
from rag_system.queue import IngestionJob, ReceivedIngestionJob
from rag_system.service import RagService
from rag_system.storage import document_record_key
from rag_system.worker import IngestionWorker


class FakeStore:
    def __init__(self, record: DocumentRecord, content: bytes = b"%PDF-1.4") -> None:
        self.objects: dict[str, object] = {
            document_record_key(record.id): record.model_dump(mode="json")
        }
        self.content = content
        self.pdf_reads: list[tuple[str, str]] = []
        self.record_statuses: list[str] = []
        self.chunks_written = False

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        if key.endswith("/record.json"):
            self.record_statuses.append(payload["status"])
        return f"s3://bucket/{key}"

    def get_pdf(self, document_id: str, version: str) -> bytes:
        self.pdf_reads.append((document_id, version))
        return self.content

    def put_chunks(self, document_id: str, version: str, chunks) -> str:
        self.chunks_written = True
        return f"s3://bucket/chunks/{document_id}/{version}/chunks.jsonl"


class FakeParser:
    async def parse(
        self,
        document_id: str,
        version: str,
        filename: str,
        content: bytes,
    ) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            version=version,
            markdown="# Intro\nbody",
            metadata={"filename": filename, "bytes": len(content)},
        )


class FailingParser:
    async def parse(self, *args, **kwargs) -> ParsedDocument:
        raise RuntimeError("parse boom")


class FakeChunker:
    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        return [
            Chunk(
                id="chunk-1",
                document_id=parsed.document_id,
                version=parsed.version,
                text=parsed.markdown,
            )
        ]


class FakeEmbedder:
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [EmbeddedChunk(chunk=chunk, dense_vector=[0.1, 0.2]) for chunk in chunks]


class FakeIndex:
    def __init__(self) -> None:
        self.upserted: list[EmbeddedChunk] = []

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        self.upserted.extend(embedded_chunks)


class FakeQueue:
    def __init__(self, messages: list[ReceivedIngestionJob]) -> None:
        self.messages = messages
        self.deleted: list[str] = []

    def receive(self) -> list[ReceivedIngestionJob]:
        return self.messages

    def delete(self, received: ReceivedIngestionJob) -> None:
        self.deleted.append(received.ack_id)


def test_worker_processes_job_and_deletes_message_after_success() -> None:
    record = _record()
    store = FakeStore(record)
    index = FakeIndex()
    service = _service(record, store, parser=FakeParser(), index=index)
    queue = FakeQueue([_received_job(record)])

    processed = asyncio.run(IngestionWorker(service, queue).process_once())

    assert processed == 1
    assert store.pdf_reads == [(record.id, record.version)]
    assert store.record_statuses == ["parsing", "chunking", "embedding", "indexed"]
    assert store.chunks_written
    assert len(index.upserted) == 1
    assert service.get_document(record.id).status == DocumentStatus.indexed
    assert queue.deleted == ["receipt-1"]


def test_worker_marks_failed_and_keeps_message_for_retry_on_failure() -> None:
    record = _record()
    store = FakeStore(record)
    service = _service(record, store, parser=FailingParser(), index=FakeIndex())
    queue = FakeQueue([_received_job(record)])

    processed = asyncio.run(IngestionWorker(service, queue).process_once())

    assert processed == 1
    final = service.get_document(record.id)
    assert final.status == DocumentStatus.failed
    assert final.error == "parse boom"
    assert store.record_statuses == ["parsing", "failed"]
    assert queue.deleted == []


def test_worker_skips_deleted_document_as_noop() -> None:
    # Regression for item 2: a job whose document was deleted before ingestion
    # is terminal — it can never succeed. The worker must delete the message as
    # a no-op rather than leaving it to redeliver until it lands in the DLQ.
    record = _record().model_copy(update={"status": DocumentStatus.deleted})
    store = FakeStore(record)
    service = _service(record, store, parser=FakeParser(), index=FakeIndex())
    queue = FakeQueue([_received_job(record)])

    processed = asyncio.run(IngestionWorker(service, queue).process_once())

    assert processed == 1
    # Message removed (not retried) and no pipeline work happened.
    assert queue.deleted == ["receipt-1"]
    assert store.record_statuses == []


def test_worker_overlaps_blocking_ingestion_stages() -> None:
    # Regression for item 1: the synchronous embed/upsert/persist stages must
    # run off the event loop (via asyncio.to_thread) so multiple messages from a
    # single poll actually overlap. The fake embedder blocks with time.sleep
    # (NOT asyncio.sleep), so if the stage ran on the loop thread the documents
    # would serialise and max_active would stay at 1.
    active = 0
    max_active = 0
    lock = threading.Lock()

    class BlockingEmbedder:
        def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return [EmbeddedChunk(chunk=c, dense_vector=[0.1, 0.2]) for c in chunks]

    records = [
        DocumentRecord(
            id=f"doc-{i}",
            title=f"f{i}.pdf",
            version="v",
            s3_uri="s3://bucket/raw/doc/v/f.pdf",
            status=DocumentStatus.queued,
        )
        for i in range(3)
    ]
    store = _MultiRecordStore(records)
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        embedding_model_id="embed-model",
        gcs_bucket="bucket",
        pinecone_index_name="index",
        ingestion_max_concurrency=4,
    )
    service._store = store
    service._documents = {}
    service._parser = FakeParser()
    service._chunker = FakeChunker()
    service._embedder = BlockingEmbedder()
    service._sparse_encoder = None
    service._index = FakeIndex()
    service._generator = None

    messages = [
        ReceivedIngestionJob(
            job=IngestionJob(
                document_id=r.id,
                version=r.version,
                filename=r.title,
                s3_uri=r.s3_uri,
            ),
            ack_id=f"receipt-{i}",
            message_id=f"message-{i}",
        )
        for i, r in enumerate(records)
    ]
    queue = FakeQueue(messages)

    processed = asyncio.run(IngestionWorker(service, queue).process_once())

    assert processed == 3
    assert len(queue.deleted) == 3
    # Blocking stages overlapped across documents instead of running serially.
    assert max_active > 1


class _MultiRecordStore:
    """Minimal multi-document store for the concurrency regression test."""

    def __init__(self, records: list[DocumentRecord]) -> None:
        self.objects: dict[str, object] = {
            document_record_key(r.id): r.model_dump(mode="json") for r in records
        }
        self._lock = threading.Lock()

    def get_json(self, key: str) -> object | None:
        with self._lock:
            return self.objects.get(key)

    def put_json(self, key: str, payload: object) -> str:
        with self._lock:
            self.objects[key] = payload
        return f"s3://bucket/{key}"

    def get_pdf(self, document_id: str, version: str) -> bytes:
        return b"%PDF-1.4"

    def put_chunks(self, document_id: str, version: str, chunks) -> str:
        return f"s3://bucket/chunks/{document_id}/{version}/chunks.jsonl"


def _record() -> DocumentRecord:
    return DocumentRecord(
        id="doc-123",
        title="source.pdf",
        version="abc",
        s3_uri="s3://bucket/raw/doc-123/abc/source.pdf",
        status=DocumentStatus.queued,
    )


def _received_job(record: DocumentRecord) -> ReceivedIngestionJob:
    return ReceivedIngestionJob(
        job=IngestionJob(
            document_id=record.id,
            version=record.version,
            filename=record.title,
            s3_uri=record.s3_uri,
        ),
        ack_id="receipt-1",
        message_id="message-1",
    )


def _service(
    record: DocumentRecord,
    store: FakeStore,
    *,
    parser,
    index: FakeIndex,
) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        sparse_enabled=False,
        embedding_model_id="embed-model",
        gcs_bucket="bucket",
        pinecone_index_name="index",
    )
    service._store = store
    service._documents = {}
    service._parser = parser
    service._chunker = FakeChunker()
    service._embedder = FakeEmbedder()
    service._sparse_encoder = None
    service._index = index
    service._generator = None
    return service
