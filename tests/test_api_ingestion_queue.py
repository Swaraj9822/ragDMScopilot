from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.models import DocumentStatus
from rag_system.service import RagService
from rag_system.storage import document_record_key


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, object] = {}
        self.uploads: list[tuple[str, str, bytes]] = []

    def put_raw(self, document_id: str, version: str, filename: str, content: bytes) -> str:
        self.uploads.append((document_id, version, content))
        suffix = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        return f"s3://bucket/raw/{document_id}/{version}/source.{suffix}"

    # Backward-compatible alias delegating to put_raw
    def put_pdf(self, document_id: str, version: str, content: bytes) -> str:
        return self.put_raw(document_id, version, "source.pdf", content)

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"


class FakeQueue:
    def __init__(self) -> None:
        self.jobs = []

    def enqueue(self, job):
        self.jobs.append(job)
        return "message-1"


class FakeSettings:
    max_upload_bytes = 1024


class TinyUploadSettings:
    max_upload_bytes = 32


def test_upload_document_queues_ingestion_without_running_pipeline(monkeypatch) -> None:
    store = FakeStore()
    queue = FakeQueue()
    service = object.__new__(RagService)
    service._store = store
    service._queue = queue
    service._documents = {}

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(api_module, "get_settings", lambda: FakeSettings())
    client = TestClient(api_module.app)

    response = client.post(
        "/documents",
        files={"file": ("source.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == DocumentStatus.queued
    assert body["title"] == "source.pdf"
    assert body["s3_uri"].startswith("s3://bucket/raw/")

    assert len(store.uploads) == 1
    document_id = body["id"]
    record_payload = store.objects[document_record_key(document_id)]
    assert record_payload["status"] == DocumentStatus.queued

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job.document_id == document_id
    assert job.version == body["version"]
    assert job.filename == "source.pdf"
    assert job.s3_uri == body["s3_uri"]

    assert not hasattr(service, "_parser")
    assert not hasattr(service, "_embedder")
    assert not hasattr(service, "_index")


def test_upload_document_rejects_file_over_size_limit(monkeypatch) -> None:
    store = FakeStore()
    queue = FakeQueue()
    service = object.__new__(RagService)
    service._store = store
    service._queue = queue
    service._documents = {}

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(api_module, "get_settings", lambda: TinyUploadSettings())
    client = TestClient(api_module.app)

    response = client.post(
        "/documents",
        files={"file": ("source.pdf", b"%PDF-1.4" + (b"x" * 64), "application/pdf")},
    )

    assert response.status_code == 413
    assert "Maximum size is 32 bytes" in response.json()["detail"]
    assert store.uploads == []
    assert queue.jobs == []


def test_upload_document_rejects_upload_when_multipart_request_exceeds_size_limit(
    monkeypatch,
) -> None:
    store = FakeStore()
    queue = FakeQueue()
    service = object.__new__(RagService)
    service._store = store
    service._queue = queue
    service._documents = {}

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(api_module, "get_settings", lambda: TinyUploadSettings())
    client = TestClient(api_module.app)

    response = client.post(
        "/documents",
        files={"file": ("source.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 413
    assert store.uploads == []
    assert queue.jobs == []


def test_update_missing_document_returns_404(monkeypatch) -> None:
    service = object.__new__(RagService)
    service._documents = {}
    service._store = type("FakeStore", (), {"get_json": lambda self, key: None})()

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(api_module, "get_settings", lambda: FakeSettings())
    client = TestClient(api_module.app)

    response = client.put(
        "/documents/missing",
        files={"file": ("source.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 404


def test_delete_missing_document_returns_404(monkeypatch) -> None:
    service = object.__new__(RagService)
    service._documents = {}
    service._store = type("FakeStore", (), {"get_json": lambda self, key: None})()

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    client = TestClient(api_module.app)

    response = client.delete("/documents/missing")

    assert response.status_code == 404
