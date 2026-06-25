from rag_system.models import DocumentStatus
from rag_system.storage import document_record_key


def test_upload_document_queues_ingestion_without_running_pipeline(
    fastapi_client, mock_store, mock_queue, test_service
) -> None:
    response = fastapi_client.post(
        "/documents",
        files={"file": ("source.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == DocumentStatus.queued
    assert body["title"] == "source.pdf"
    assert body["s3_uri"].startswith("s3://bucket/raw/")

    assert len(mock_store.uploads) == 1
    document_id = body["id"]
    record_payload = mock_store.objects[document_record_key(document_id)]
    assert record_payload["status"] == DocumentStatus.queued

    assert len(mock_queue.jobs) == 1
    job = mock_queue.jobs[0]
    assert job.document_id == document_id
    assert job.version == body["version"]
    assert job.filename == "source.pdf"
    assert job.s3_uri == body["s3_uri"]

    assert not hasattr(test_service, "_parser")
    assert not hasattr(test_service, "_embedder")
    assert not hasattr(test_service, "_index")


def test_upload_document_rejects_file_over_size_limit(
    fastapi_client, test_settings, mock_store, mock_queue
) -> None:
    test_settings.max_upload_bytes = 32

    response = fastapi_client.post(
        "/documents",
        files={"file": ("source.pdf", b"%PDF-1.4" + (b"x" * 64), "application/pdf")},
    )

    assert response.status_code == 413
    assert "Maximum size is 32 bytes" in response.json()["detail"]
    assert mock_store.uploads == []
    assert mock_queue.jobs == []


def test_upload_document_rejects_upload_when_multipart_request_exceeds_size_limit(
    fastapi_client, test_settings, mock_store, mock_queue
) -> None:
    test_settings.max_upload_bytes = 32

    response = fastapi_client.post(
        "/documents",
        files={"file": ("source.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 413
    assert mock_store.uploads == []
    assert mock_queue.jobs == []


def test_update_missing_document_returns_404(fastapi_client) -> None:
    missing_id = "00000000-0000-0000-0000-000000000000"
    response = fastapi_client.put(
        f"/documents/{missing_id}",
        files={"file": ("source.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 404


def test_delete_missing_document_returns_404(fastapi_client) -> None:
    missing_id = "00000000-0000-0000-0000-000000000000"
    response = fastapi_client.delete(f"/documents/{missing_id}")

    assert response.status_code == 404
