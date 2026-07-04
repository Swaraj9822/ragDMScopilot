"""Tests for the P1 throughput/latency changes.

Covers:
- Finding 1: embeddings are issued concurrently but returned in input order.
- Finding 5: Pinecone upserts are split into bounded batches.
- Finding 3: list_documents fetches records concurrently and still orders them.
- Finding 10: the ingestion worker drains a multi-message poll concurrently.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from rag_system.embedding import GeminiEmbedder
from rag_system.models import Chunk, DocumentRecord, DocumentStatus, EmbeddedChunk
from rag_system.retrieval import PineconeHybridIndex
from rag_system.service import RagService
from rag_system.storage import document_record_key


# ---------------------------------------------------------------------------
# Finding 1 — concurrent embedding, order preserved.
# ---------------------------------------------------------------------------


def _embedder(max_workers: int) -> GeminiEmbedder:
    embedder = object.__new__(GeminiEmbedder)
    embedder._client = None
    embedder._model_id = "gemini-embedding-test"
    embedder._dimension = 3
    embedder._max_workers = max_workers
    return embedder


def test_embed_chunks_preserves_input_order_under_concurrency() -> None:
    embedder = _embedder(max_workers=8)

    # Map each text to a deterministic, distinct vector so we can detect any
    # reordering introduced by the concurrent fan-out.
    def fake_embed_single(text: str) -> list[float]:
        n = float(text.split("-")[1])
        return [n, n, n]

    embedder._embed_single = fake_embed_single  # type: ignore[method-assign]

    chunks = [
        Chunk(id=f"c{i}", document_id="doc", version="v", text=f"chunk-{i}")
        for i in range(50)
    ]
    embedded = embedder.embed_chunks(chunks)

    assert [e.dense_vector[0] for e in embedded] == [float(i) for i in range(50)]
    assert all(e.chunk is c for e, c in zip(embedded, chunks))


def test_embed_runs_calls_concurrently() -> None:
    embedder = _embedder(max_workers=8)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def slow_embed_single(text: str) -> list[float]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return [0.0, 0.0, 0.0]

    embedder._embed_single = slow_embed_single  # type: ignore[method-assign]

    embedder._embed([f"chunk-{i}" for i in range(16)])

    # With a serial loop this would peak at 1; concurrency should overlap calls.
    assert max_active > 1


def test_embedder_reuses_instance_pool_across_calls() -> None:
    # Item 5: the embed pool is created once and reused, not rebuilt per call.
    embedder = _embedder(max_workers=8)
    embedder._embed_single = lambda text: [0.0, 0.0, 0.0]  # type: ignore[method-assign]

    embedder._embed(["a", "b"])
    first_pool = embedder._executor
    assert first_pool is not None

    embedder._embed(["c", "d"])
    assert embedder._executor is first_pool


def test_single_worker_setting_embeds_serially() -> None:
    embedder = _embedder(max_workers=1)
    embedder._embed_single = lambda text: [1.0, 1.0, 1.0]  # type: ignore[method-assign]

    result = embedder._embed(["chunk-0", "chunk-1", "chunk-2"])
    assert result == [[1.0, 1.0, 1.0]] * 3


# ---------------------------------------------------------------------------
# Finding 5 — Pinecone upsert batching.
# ---------------------------------------------------------------------------


class RecordingPineconeIndex:
    def __init__(self) -> None:
        self.upsert_calls: list[int] = []

    def upsert(self, vectors) -> None:
        self.upsert_calls.append(len(vectors))


def _hybrid_index(batch_size: int) -> tuple[PineconeHybridIndex, RecordingPineconeIndex]:
    index = object.__new__(PineconeHybridIndex)
    raw = RecordingPineconeIndex()
    index._index = raw
    index._upsert_batch_size = batch_size
    return index, raw


def _embedded(n: int) -> list[EmbeddedChunk]:
    return [
        EmbeddedChunk(
            chunk=Chunk(id=f"c{i}", document_id="doc", version="v", text="t"),
            dense_vector=[0.1, 0.2, 0.3],
        )
        for i in range(n)
    ]


def test_upsert_splits_into_bounded_batches() -> None:
    index, raw = _hybrid_index(batch_size=100)
    index.upsert(_embedded(250))
    assert raw.upsert_calls == [100, 100, 50]


def test_upsert_single_batch_when_under_limit() -> None:
    index, raw = _hybrid_index(batch_size=100)
    index.upsert(_embedded(40))
    assert raw.upsert_calls == [40]


def test_upsert_empty_does_nothing() -> None:
    index, raw = _hybrid_index(batch_size=100)
    index.upsert([])
    assert raw.upsert_calls == []


def test_upsert_retries_only_failing_batch(monkeypatch) -> None:
    # A transient failure must re-send only the offending batch, not re-run the
    # batches that already succeeded (the retry lives on _upsert_batch, not on
    # upsert). Silence tenacity's back-off so the test stays fast.
    monkeypatch.setattr(
        PineconeHybridIndex._upsert_batch.retry, "sleep", lambda *a, **k: None
    )

    class FlakyIndex:
        def __init__(self) -> None:
            self.upsert_calls: list[int] = []
            self._failed = False

        def upsert(self, vectors) -> None:
            self.upsert_calls.append(len(vectors))
            # Fail once on the final (50-vector) batch, then succeed on retry.
            if len(vectors) == 50 and not self._failed:
                self._failed = True
                raise RuntimeError("transient pinecone error")

    index = object.__new__(PineconeHybridIndex)
    raw = FlakyIndex()
    index._index = raw
    index._upsert_batch_size = 100

    index.upsert(_embedded(250))

    # Batches 1 and 2 ran exactly once; only the failing 50-batch was retried.
    # A whole-method retry would have re-run the two 100-vector batches.
    assert raw.upsert_calls == [100, 100, 50, 50]


# ---------------------------------------------------------------------------
# Finding 3 — concurrent list_documents.
# ---------------------------------------------------------------------------


class ListStore:
    def __init__(self, records: list[DocumentRecord]) -> None:
        self.objects = {
            document_record_key(r.id): r.model_dump(mode="json") for r in records
        }
        self.concurrent_reads = 0
        self._active = 0
        self._lock = threading.Lock()

    def list_document_record_keys(self) -> list[str]:
        return list(self.objects.keys())

    def get_json(self, key: str) -> object | None:
        with self._lock:
            self._active += 1
            self.concurrent_reads = max(self.concurrent_reads, self._active)
        time.sleep(0.01)
        with self._lock:
            self._active -= 1
        return self.objects.get(key)


def _list_service(store: ListStore, max_workers: int) -> RagService:
    service = object.__new__(RagService)
    service._store = store
    service._documents = {}
    service._settings = SimpleNamespace(document_list_max_workers=max_workers)
    return service


def test_list_documents_returns_all_sorted_by_title() -> None:
    records = [
        DocumentRecord(id="d1", title="Zebra", version="v", s3_uri="s3://b/1", status=DocumentStatus.indexed),
        DocumentRecord(id="d2", title="apple", version="v", s3_uri="s3://b/2", status=DocumentStatus.indexed),
        DocumentRecord(id="d3", title="Mango", version="v", s3_uri="s3://b/3", status=DocumentStatus.indexed),
    ]
    store = ListStore(records)
    service = _list_service(store, max_workers=8)

    listed = service.list_documents()

    assert [r.title for r in listed] == ["apple", "Mango", "Zebra"]
    # The per-document reads overlapped rather than running strictly serially.
    assert store.concurrent_reads > 1


def test_list_documents_reuses_instance_pool() -> None:
    # Item 5: the Documents-listing pool is created once and reused across calls.
    records = [
        DocumentRecord(id=f"d{i}", title=f"t{i}", version="v", s3_uri=f"s3://b/{i}", status=DocumentStatus.indexed)
        for i in range(3)
    ]
    service = _list_service(ListStore(records), max_workers=8)

    service.list_documents()
    first_pool = service._document_list_executor
    assert first_pool is not None

    service.list_documents()
    assert service._document_list_executor is first_pool


def test_list_documents_empty_corpus() -> None:
    service = _list_service(ListStore([]), max_workers=8)
    assert service.list_documents() == []


# ---------------------------------------------------------------------------
# Finding 10 — concurrent worker draining.
# ---------------------------------------------------------------------------


def test_worker_processes_batch_concurrently() -> None:
    from rag_system.worker import IngestionWorker

    active = 0
    max_active = 0
    lock = threading.Lock()

    class SlowService:
        # Exposes the public ``settings`` property that RagService now provides
        # (the worker reads config through it instead of the private attr).
        settings = SimpleNamespace(ingestion_max_concurrency=4)

        async def process_document_job(self, job) -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            with lock:
                active -= 1

    class BatchQueue:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def receive(self):
            return [
                SimpleNamespace(
                    job=SimpleNamespace(
                        document_id=f"doc-{i}", version="v", trace_id=None
                    ),
                    receipt_handle=f"r-{i}",
                )
                for i in range(6)
            ]

        def delete(self, message) -> None:
            self.deleted.append(message.receipt_handle)

    queue = BatchQueue()
    worker = IngestionWorker(SlowService(), queue)

    processed = asyncio.run(worker.process_once())

    assert processed == 6
    assert len(queue.deleted) == 6
    # Bounded at the configured concurrency, but strictly greater than serial.
    assert 1 < max_active <= 4


def test_worker_defaults_to_sequential_without_settings() -> None:
    from rag_system.worker import IngestionWorker

    order: list[str] = []

    class SeqService:
        async def process_document_job(self, job) -> None:
            order.append(job.document_id)

    class Q:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def receive(self):
            return [
                SimpleNamespace(
                    job=SimpleNamespace(document_id=f"doc-{i}", version="v", trace_id=None),
                    receipt_handle=f"r-{i}",
                )
                for i in range(3)
            ]

        def delete(self, message) -> None:
            self.deleted.append(message.receipt_handle)

    worker = IngestionWorker(SeqService(), Q())
    processed = asyncio.run(worker.process_once())

    assert processed == 3
    assert order == ["doc-0", "doc-1", "doc-2"]
