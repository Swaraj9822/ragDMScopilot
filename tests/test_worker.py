import asyncio
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
        self.deleted.append(received.receipt_handle)


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
        receipt_handle="receipt-1",
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
        bedrock_embedding_model_id="embed-model",
        s3_bucket="bucket",
        pinecone_index_name="index",
    )
    service._store = store
    service._documents = {}
    service._parser = parser
    service._chunker = FakeChunker()
    service._embedder = FakeEmbedder()
    service._sparse_encoder = None
    service._index = index
    service._reranker = None
    service._generator = None
    return service
