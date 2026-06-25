from rag_system.models import DocumentRecord, DocumentStatus
from rag_system.service import RagService


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def put_json(self, key: str, payload: object) -> str:
        self.objects[key] = payload
        return f"s3://bucket/{key}"

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)


def test_document_record_is_persisted_and_reloaded_from_store() -> None:
    service = object.__new__(RagService)
    service._store = FakeStore()

    record = DocumentRecord(
        id="doc-123",
        title="source.pdf",
        version="abc",
        s3_uri="s3://bucket/raw/doc-123/abc/source.pdf",
        status=DocumentStatus.indexed,
    )

    service._save_document_record(record)

    reloaded = service.get_document("doc-123")

    assert reloaded == record
