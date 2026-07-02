import json
from collections.abc import Iterable

from botocore.exceptions import ClientError

from rag_system.config import Settings
from rag_system.models import Chunk
from rag_system.observability import get_logger, retry_on_transient

logger = get_logger(__name__)


class PreconditionFailed(Exception):
    """Raised when a conditional S3 write fails its ETag precondition.

    Signals that another writer modified the object between our read and write,
    so the caller should reload the current state and decide whether to retry.
    """


class S3ArtifactStore:
    def __init__(self, settings: Settings):
        self._bucket = settings.s3_bucket
        self._kms_key_id = settings.s3_kms_key_id
        self._client = settings.boto3_session().client(
            "s3", config=settings.boto3_client_config()
        )
        logger.info("S3ArtifactStore initialised (bucket=%s)", self._bucket)

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_raw(self, document_id: str, version: str, filename: str, content: bytes) -> str:
        key = raw_document_key(document_id, version, filename)
        logger.info(
            "Uploading document to s3://%s/%s (%d bytes)",
            self._bucket,
            key,
            len(content),
            extra={"document_id": document_id, "version": version, "s3_key": key},
        )
        self._put_bytes(key, content, "application/octet-stream")
        return f"s3://{self._bucket}/{key}"

    # Backward-compatible alias
    def put_pdf(self, document_id: str, version: str, content: bytes, filename: str = "document.pdf") -> str:
        return self.put_raw(document_id, version, filename, content)

    def get_raw(self, document_id: str, version: str, filename: str) -> bytes:
        return self.get_bytes(raw_document_key(document_id, version, filename))

    def get_pdf(self, document_id: str, version: str) -> bytes:
        return self.get_bytes(raw_document_key(document_id, version, "source.pdf"))

    def put_json(self, key: str, payload: object) -> str:
        logger.debug("Writing JSON to s3://%s/%s", self._bucket, key, extra={"s3_key": key})
        self._put_bytes(
            key,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        )
        return f"s3://{self._bucket}/{key}"

    @retry_on_transient()
    def get_json(self, key: str) -> object | None:
        logger.debug("Reading JSON from s3://%s/%s", self._bucket, key, extra={"s3_key": key})
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"NoSuchKey", "404", "NotFound"}:
                logger.info("S3 JSON object not found: %s", key, extra={"s3_key": key})
                return None
            raise
        return json.loads(response["Body"].read().decode("utf-8"))

    @retry_on_transient()
    def get_json_with_etag(self, key: str) -> tuple[object | None, str | None]:
        """Read a JSON object together with its S3 ETag.

        Returns ``(payload, etag)`` on hit and ``(None, None)`` when the object
        does not exist. The ETag feeds :meth:`put_json_conditional` for
        optimistic-concurrency writes.
        """
        logger.debug("Reading JSON+etag from s3://%s/%s", self._bucket, key, extra={"s3_key": key})
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"NoSuchKey", "404", "NotFound"}:
                return None, None
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload, response.get("ETag")

    def put_json_conditional(
        self,
        key: str,
        payload: object,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        """Write JSON only if the object's current state matches a precondition.

        - ``if_match``: succeed only if the object's current ETag equals this
          value (i.e. it has not changed since we read it).
        - ``if_none_match``: succeed only if the object does not yet exist
          (create-only).

        Raises :class:`PreconditionFailed` (HTTP 412) when the precondition is
        not met, so the caller can reload and re-evaluate. Transient errors are
        retried, but a 412 is deterministic and must not be.
        """
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        extra: dict[str, str] = {}
        if if_none_match:
            extra["IfNoneMatch"] = "*"
        elif if_match:
            extra["IfMatch"] = if_match
        self._put_bytes_conditional(key, content, "application/json", extra)

    @retry_on_transient()
    def _put_bytes_conditional(
        self, key: str, content: bytes, content_type: str, conditions: dict[str, str]
    ) -> None:
        kwargs = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": content,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms" if self._kms_key_id else "AES256",
            **conditions,
        }
        if self._kms_key_id:
            kwargs["SSEKMSKeyId"] = self._kms_key_id
        try:
            self._client.put_object(**kwargs)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error_code == "PreconditionFailed" or status == 412:
                logger.info(
                    "Conditional S3 write rejected (precondition failed): %s",
                    key,
                    extra={"s3_key": key},
                )
                raise PreconditionFailed(key) from exc
            raise
        logger.debug(
            "Conditional S3 put complete: %s (%d bytes)", key, len(content), extra={"s3_key": key}
        )

    def put_chunks(self, document_id: str, version: str, chunks: Iterable[Chunk]) -> str:
        lines = [chunk.model_dump_json() for chunk in chunks]
        key = chunks_key(document_id, version)
        logger.info(
            "Writing %d chunks to s3://%s/%s",
            len(lines),
            self._bucket,
            key,
            extra={
                "document_id": document_id,
                "version": version,
                "s3_key": key,
                "chunk_count": len(lines),
            },
        )
        self._put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"), "application/x-ndjson")
        return f"s3://{self._bucket}/{key}"

    @retry_on_transient()
    def get_bytes(self, key: str) -> bytes:
        logger.debug("Reading bytes from s3://%s/%s", self._bucket, key, extra={"s3_key": key})
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    @retry_on_transient()
    def _put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        kwargs = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": content,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms" if self._kms_key_id else "AES256",
        }
        if self._kms_key_id:
            kwargs["SSEKMSKeyId"] = self._kms_key_id
        self._client.put_object(**kwargs)
        logger.debug("S3 put complete: %s (%d bytes)", key, len(content), extra={"s3_key": key})

    def list_document_record_keys(self) -> list[str]:
        """Return the S3 keys of all persisted document records."""
        keys: list[str] = []
        prefix = "documents/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if key.endswith("/record.json"):
                    keys.append(key)
        return keys


def raw_document_key(document_id: str, version: str, filename: str) -> str:
    """Return the S3 key for a raw uploaded document, preserving original extension."""
    from pathlib import Path
    suffix = Path(filename).suffix or ".bin"
    return f"raw/{document_id}/{version}/source{suffix}"


# Backward-compatible alias
def raw_pdf_key(document_id: str, version: str) -> str:
    return raw_document_key(document_id, version, "source.pdf")


def parsed_key(document_id: str, version: str) -> str:
    return f"parsed/{document_id}/{version}/llamaparse.json"


def chunks_key(document_id: str, version: str) -> str:
    return f"chunks/{document_id}/{version}/chunks.jsonl"


def embedding_manifest_key(document_id: str, version: str) -> str:
    return f"embeddings/{document_id}/{version}/manifest.json"


def document_record_key(document_id: str) -> str:
    return f"documents/{document_id}/record.json"


def query_trace_key(trace_id: str) -> str:
    return f"queries/{trace_id}/trace.json"


def query_feedback_key(trace_id: str, feedback_id: str) -> str:
    return f"queries/{trace_id}/feedback/{feedback_id}.json"
