import json
from collections.abc import Iterable

from botocore.exceptions import ClientError

from rag_system.config import Settings
from rag_system.models import Chunk
from rag_system.observability import get_logger, retry_on_transient

logger = get_logger(__name__)


class S3ArtifactStore:
    def __init__(self, settings: Settings):
        self._bucket = settings.s3_bucket
        self._kms_key_id = settings.s3_kms_key_id
        self._client = settings.boto3_session().client("s3")
        logger.info("S3ArtifactStore initialised (bucket=%s)", self._bucket)

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_pdf(self, document_id: str, version: str, content: bytes) -> str:
        key = raw_pdf_key(document_id, version)
        logger.info(
            "Uploading PDF to s3://%s/%s (%d bytes)",
            self._bucket,
            key,
            len(content),
            extra={"document_id": document_id, "version": version, "s3_key": key},
        )
        self._put_bytes(key, content, "application/pdf")
        return f"s3://{self._bucket}/{key}"

    def get_pdf(self, document_id: str, version: str) -> bytes:
        return self.get_bytes(raw_pdf_key(document_id, version))

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


def raw_pdf_key(document_id: str, version: str) -> str:
    return f"raw/{document_id}/{version}/source.pdf"


def parsed_key(document_id: str, version: str) -> str:
    return f"parsed/{document_id}/{version}/llamaparse.json"


def chunks_key(document_id: str, version: str) -> str:
    return f"chunks/{document_id}/{version}/chunks.jsonl"


def embedding_manifest_key(document_id: str, version: str) -> str:
    return f"embeddings/{document_id}/{version}/manifest.json"


def document_record_key(document_id: str) -> str:
    return f"documents/{document_id}/record.json"
