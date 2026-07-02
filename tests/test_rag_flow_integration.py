import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.models import (
    Chunk,
    Citation,
    DocumentStatus,
    EmbeddedChunk,
    ParsedDocument,
    QueryResponse,
    RetrievalHit,
)
from rag_system.queue import ReceivedIngestionJob
from rag_system.service import RagService
from rag_system.storage import (
    chunks_key,
    document_record_key,
    parsed_key,
    query_feedback_key,
    query_trace_key,
    raw_document_key,
)
from rag_system.worker import IngestionWorker


class FakeSettings:
    max_upload_bytes = 1024
    sparse_enabled = False
    rerank_enabled = False
    rerank_top_k = 12
    retrieval_dense_top_k = 10
    low_top_score_threshold = None
    bedrock_embedding_model_id = "fake-embedder"
    s3_bucket = "bucket"
    pinecone_index_name = "fake-index"


class IntegrationStore:
    def __init__(self) -> None:
        self.objects: dict[str, object] = {}
        self.bytes: dict[str, bytes] = {}

    def put_raw(self, document_id: str, version: str, filename: str, content: bytes) -> str:
        key = raw_document_key(document_id, version, filename)
        self.bytes[key] = content
        return f"s3://bucket/{key}"

    def get_raw(self, document_id: str, version: str, filename: str) -> bytes:
        return self.bytes[raw_document_key(document_id, version, filename)]

    # Backward-compatible aliases matching the production storage interface.
    def put_pdf(self, document_id: str, version: str, content: bytes) -> str:
        return self.put_raw(document_id, version, "source.pdf", content)

    def get_pdf(self, document_id: str, version: str) -> bytes:
        return self.get_raw(document_id, version, "source.pdf")

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def put_chunks(self, document_id: str, version: str, chunks: list[Chunk]) -> str:
        self.objects[chunks_key(document_id, version)] = [
            chunk.model_dump(mode="json") for chunk in chunks
        ]
        return f"s3://bucket/{chunks_key(document_id, version)}"


class IntegrationQueue:
    def __init__(self) -> None:
        self.jobs = []
        self.deleted: list[str] = []

    def enqueue(self, job) -> str:
        self.jobs.append(job)
        return f"message-{len(self.jobs)}"

    def receive(self) -> list[ReceivedIngestionJob]:
        return [
            ReceivedIngestionJob(
                job=job,
                receipt_handle=f"receipt-{index}",
                message_id=f"message-{index}",
            )
            for index, job in enumerate(self.jobs, start=1)
            if f"receipt-{index}" not in self.deleted
        ]

    def delete(self, received: ReceivedIngestionJob) -> None:
        self.deleted.append(received.receipt_handle)


class IntegrationParser:
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
            markdown="# Revenue\nRevenue was 10 on page 2.",
            metadata={"source_filename": filename, "bytes": len(content)},
        )


class IntegrationChunker:
    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        return [
            Chunk(
                id=f"{parsed.document_id}:chunk-1",
                document_id=parsed.document_id,
                version=parsed.version,
                text="Revenue was 10 on page 2.",
                page_start=2,
                page_end=2,
                section_path=["Revenue"],
                metadata={"source_filename": parsed.metadata["source_filename"]},
            )
        ]


class IntegrationEmbedder:
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [EmbeddedChunk(chunk=chunk, dense_vector=[0.1, 0.2, 0.3]) for chunk in chunks]

    def embed_query(self, question: str) -> list[float]:
        assert "revenue" in question.lower()
        return [0.1, 0.2, 0.3]


class IntegrationIndex:
    def __init__(self) -> None:
        self.upserted: list[EmbeddedChunk] = []
        self.searches: list[dict[str, object]] = []
        self.deleted_document_ids: list[str] = []

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        document_ids = {item.chunk.document_id for item in embedded_chunks}
        self.upserted = [
            item for item in self.upserted if item.chunk.document_id not in document_ids
        ]
        self.upserted.extend(embedded_chunks)

    def delete_document(self, document_id: str) -> None:
        self.deleted_document_ids.append(document_id)
        self.upserted = [
            item for item in self.upserted if item.chunk.document_id != document_id
        ]

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        document_ids: list[str] | None = None,
        sparse_vector: dict | None = None,
    ) -> list[RetrievalHit]:
        self.searches.append(
            {
                "query_vector": query_vector,
                "top_k": top_k,
                "document_ids": document_ids,
                "sparse_vector": sparse_vector,
            }
        )
        hits = [
            RetrievalHit(chunk=item.chunk, score=0.99, source="fake-pinecone")
            for item in self.upserted
        ]
        if document_ids:
            hits = [hit for hit in hits if hit.chunk.document_id in document_ids]
        return hits[:top_k]


class IntegrationGenerator:
    def answer(self, question: str, hits: list[RetrievalHit], trace_id: str) -> QueryResponse:
        return QueryResponse(
            answer="Revenue was 10.",
            citations=[
                Citation(
                    document_id=hit.chunk.document_id,
                    chunk_id=hit.chunk.id,
                    page_start=hit.chunk.page_start,
                    page_end=hit.chunk.page_end,
                    title=hit.chunk.metadata.get("source_filename"),
                )
                for hit in hits
            ],
            evidence_status="grounded" if hits else "insufficient_evidence",
            trace_id=trace_id,
        )


def test_upload_worker_query_flow_with_mocked_external_systems(monkeypatch) -> None:
    store = IntegrationStore()
    queue = IntegrationQueue()
    index = IntegrationIndex()
    service = _service(store, queue, index)

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(api_module, "get_settings", lambda: FakeSettings())
    client = TestClient(api_module.app)

    upload_response = client.post(
        "/documents",
        files={"file": ("report.pdf", b"%PDF-1.4 fake report", "application/pdf")},
    )

    assert upload_response.status_code == 202
    uploaded = upload_response.json()
    assert uploaded["status"] == DocumentStatus.queued
    assert len(queue.jobs) == 1

    processed = asyncio.run(IngestionWorker(service, queue).process_once())

    assert processed == 1
    assert queue.deleted == ["receipt-1"]
    assert len(index.upserted) == 1

    document_id = uploaded["id"]
    record = store.objects[document_record_key(document_id)]
    assert record["status"] == DocumentStatus.indexed
    assert record["error"] is None
    assert parsed_key(document_id, uploaded["version"]) in store.objects
    assert chunks_key(document_id, uploaded["version"]) in store.objects

    status_response = client.get(f"/documents/{document_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == DocumentStatus.indexed

    query_response = client.post(
        "/query",
        json={"question": "What was revenue?", "document_ids": [document_id]},
    )

    assert query_response.status_code == 200
    answer = query_response.json()
    assert answer["answer"] == "Revenue was 10."
    assert answer["evidence_status"] == "grounded"
    assert answer["citations"] == [
        {
            "document_id": document_id,
            "chunk_id": f"{document_id}:chunk-1",
            "page_start": 2,
            "page_end": 2,
            "title": "report.pdf",
        }
    ]
    assert index.searches[0]["document_ids"] == [document_id]

    trace_id = answer["trace_id"]
    trace_response = client.get(f"/queries/{trace_id}")
    assert trace_response.status_code == 200
    trace = trace_response.json()
    assert trace["trace_id"] == trace_id
    assert trace["question"] == "What was revenue?"
    assert trace["route"] == "rag"
    assert trace["retrieval_mode"] == "dense"
    assert trace["answer"] == "Revenue was 10."
    assert trace["citations"][0]["chunk_id"] == f"{document_id}:chunk-1"
    assert trace["retrieved_hits"][0]["chunk_id"] == f"{document_id}:chunk-1"
    assert query_trace_key(trace_id) in store.objects

    feedback_response = client.post(
        f"/queries/{trace_id}/feedback",
        json={"rating": 5, "comment": "Looks right."},
    )
    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["trace_id"] == trace_id
    assert feedback["rating"] == 5
    assert query_feedback_key(trace_id, feedback["feedback_id"]) in store.objects


def test_delete_document_marks_deleted_and_removes_vectors(monkeypatch) -> None:
    service, client, queue, index = _wired_app(monkeypatch)
    document_id, _version = _upload_and_process(client, service, queue)

    response = client.delete(f"/documents/{document_id}")

    assert response.status_code == 200
    assert response.json()["status"] == DocumentStatus.deleted
    assert index.deleted_document_ids == [document_id]
    assert index.upserted == []

    status_response = client.get(f"/documents/{document_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == DocumentStatus.deleted

    query_response = client.post(
        "/query",
        json={"question": "What was revenue?", "document_ids": [document_id]},
    )
    assert query_response.status_code == 200
    assert query_response.json()["evidence_status"] == "insufficient_evidence"
    assert query_response.json()["citations"] == []


def test_update_document_keeps_id_queues_new_version_and_replaces_vectors(monkeypatch) -> None:
    service, client, queue, index = _wired_app(monkeypatch)
    document_id, first_version = _upload_and_process(client, service, queue)

    response = client.put(
        f"/documents/{document_id}",
        files={"file": ("updated.pdf", b"%PDF-1.4 updated report", "application/pdf")},
    )

    assert response.status_code == 202
    updated = response.json()
    assert updated["id"] == document_id
    assert updated["title"] == "updated.pdf"
    assert updated["status"] == DocumentStatus.queued
    assert updated["version"] != first_version
    # Replacement no longer pre-deletes the old vectors: the previously
    # published version stays searchable until the new version is fully ingested
    # and atomically published (then superseded vectors are cleaned up).
    assert index.deleted_document_ids == []

    processed = asyncio.run(IngestionWorker(service, queue).process_once())

    assert processed == 1
    assert len(index.upserted) == 1
    assert index.upserted[0].chunk.document_id == document_id
    assert index.upserted[0].chunk.version == updated["version"]
    assert service.get_document(document_id).status == DocumentStatus.indexed


def test_query_trace_and_feedback_missing_trace_return_404(monkeypatch) -> None:
    _service, client, _queue, _index = _wired_app(monkeypatch)

    trace_response = client.get("/queries/missing-trace")
    assert trace_response.status_code == 404

    feedback_response = client.post(
        "/queries/missing-trace/feedback",
        json={"rating": 4, "comment": "Cannot attach without trace."},
    )
    assert feedback_response.status_code == 404


def _service(store: IntegrationStore, queue: IntegrationQueue, index: IntegrationIndex) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(**FakeSettings.__dict__)
    service._store = store
    service._queue = queue
    service._documents = {}
    service._parser = IntegrationParser()
    service._chunker = IntegrationChunker()
    service._embedder = IntegrationEmbedder()
    service._sparse_encoder = None
    service._index = index
    service._reranker = None
    service._generator = IntegrationGenerator()
    return service


def _wired_app(monkeypatch):
    store = IntegrationStore()
    queue = IntegrationQueue()
    index = IntegrationIndex()
    service = _service(store, queue, index)
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(api_module, "get_settings", lambda: FakeSettings())
    client = TestClient(api_module.app)
    return service, client, queue, index


def _upload_and_process(
    client: TestClient,
    service: RagService,
    queue: IntegrationQueue,
) -> tuple[str, str]:
    response = client.post(
        "/documents",
        files={"file": ("report.pdf", b"%PDF-1.4 fake report", "application/pdf")},
    )
    assert response.status_code == 202
    body = response.json()
    asyncio.run(IngestionWorker(service, queue).process_once())
    return body["id"], body["version"]
