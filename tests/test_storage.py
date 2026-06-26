from rag_system.storage import (
    chunks_key,
    document_record_key,
    embedding_manifest_key,
    parsed_key,
    query_feedback_key,
    query_trace_key,
    raw_pdf_key,
)


def test_s3_keys_are_stable() -> None:
    document_id = "doc-123"
    version = "abc"

    assert raw_pdf_key(document_id, version) == "raw/doc-123/abc/source.pdf"
    assert parsed_key(document_id, version) == "parsed/doc-123/abc/llamaparse.json"
    assert chunks_key(document_id, version) == "chunks/doc-123/abc/chunks.jsonl"
    assert embedding_manifest_key(document_id, version) == "embeddings/doc-123/abc/manifest.json"
    assert document_record_key(document_id) == "documents/doc-123/record.json"
    assert query_trace_key("trace-123") == "queries/trace-123/trace.json"
    assert query_feedback_key("trace-123", "feedback-1") == (
        "queries/trace-123/feedback/feedback-1.json"
    )
