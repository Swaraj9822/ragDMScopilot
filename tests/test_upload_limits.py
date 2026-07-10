"""Upload size enforcement (finding #10).

The upload handler reads the body in bounded chunks and rejects it as soon as it
exceeds ``max_upload_bytes``, rather than buffering the whole (possibly
oversized) body first. These tests drive the endpoint with a tiny configured
limit so an over-limit upload is rejected with 413 and an empty one with 400.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module


class _FakeService:
    async def queue_pdf(self, filename: str, content: bytes, owner: str | None = None):
        # A successful small upload should reach here; return a minimal record.
        from rag_system.models import DocumentRecord, DocumentStatus

        return DocumentRecord(
            id="doc-1",
            title=filename,
            version="v1",
            s3_uri="s3://b/doc-1",
            status=DocumentStatus.queued,
            owner=owner,
        )


@pytest.fixture
def client(monkeypatch):
    # _read_document_upload calls get_settings() directly. The limit must exceed
    # the multipart envelope overhead of a small file (the Content-Length header
    # covers the whole request body, not just the file), so use a modest limit
    # and an over-limit payload well above it.
    monkeypatch.setattr(
        api_module, "get_settings", lambda: SimpleNamespace(max_upload_bytes=2000)
    )
    monkeypatch.setattr(api_module, "get_service", lambda: _FakeService())
    return TestClient(api_module.app)


def test_oversized_upload_rejected_with_413(client) -> None:
    resp = client.post(
        "/documents",
        files={"file": ("big.pdf", b"x" * 8000, "application/pdf")},
    )
    assert resp.status_code == 413


def test_empty_upload_rejected_with_400(client) -> None:
    resp = client.post(
        "/documents",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert resp.status_code == 400


def test_small_upload_accepted(client) -> None:
    resp = client.post(
        "/documents",
        files={"file": ("ok.pdf", b"hello", "application/pdf")},
    )
    assert resp.status_code == 202
