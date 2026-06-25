import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.config import Settings
from rag_system.service import RagService


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, object] = {}
        self.uploads: list[tuple[str, str, bytes]] = []

    def put_raw(self, document_id: str, version: str, filename: str, content: bytes) -> str:
        self.uploads.append((document_id, version, content))
        return f"s3://bucket/raw/{document_id}/{version}/{filename}"

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class FakeQueue:
    def __init__(self) -> None:
        self.jobs = []

    def enqueue(self, job):
        self.jobs.append(job)
        return "message-1"


@pytest.fixture
def mock_store():
    return FakeStore()


@pytest.fixture
def mock_queue():
    return FakeQueue()


@pytest.fixture
def test_settings():
    settings = Settings.model_construct(
        max_upload_bytes=1024,
        cors_allowed_origins="http://localhost:3000",
        s3_bucket="my-test-bucket",
        ingestion_queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
        secrets_manager_secret_id="",
    )
    return settings


@pytest.fixture
def test_service(mock_store, mock_queue, test_settings):
    service = object.__new__(RagService)
    service._store = mock_store
    service._queue = mock_queue
    service._settings = test_settings
    return service


@pytest.fixture
def fastapi_client(monkeypatch, test_settings, test_service):
    monkeypatch.setattr(api_module, "get_service", lambda: test_service)
    monkeypatch.setattr(api_module, "get_settings", lambda: test_settings)

    # We should also mock get_copilot_service to return None or a mock
    # because router depends on it.
    monkeypatch.setattr(api_module, "get_copilot_service", lambda: None)

    return TestClient(api_module.app)
