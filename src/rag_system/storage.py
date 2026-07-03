import json
from collections.abc import Callable, Iterable

from google.api_core import exceptions as gcloud_exceptions

from rag_system.config import Settings
from rag_system.models import Chunk
from rag_system.observability import get_logger, retry_on_transient

logger = get_logger(__name__)


class PreconditionFailed(Exception):
    """Raised when a conditional write fails its generation precondition.

    Signals that another writer modified the object between our read and write,
    so the caller should reload the current state and decide whether to retry.
    """


class GcsArtifactStore:
    """Artifact store backed by Google Cloud Storage.

    Optimistic concurrency uses GCS *object generations*: a create-only write
    passes ``if_generation_match=0`` (succeeds only if the object does not yet
    exist) and a compare-and-set write passes the generation observed at read
    time. The generation is surfaced to callers through :meth:`get_json_with_etag`
    as an opaque string "etag", preserving the previous S3 ETag contract.
    """

    def __init__(self, settings: Settings):
        self._bucket = settings.gcs_bucket
        self._kms_key_name = settings.gcs_kms_key_name
        self._client = settings.gcs_client()
        self._bucket_obj = self._client.bucket(self._bucket)
        logger.info("GcsArtifactStore initialised (bucket=%s)", self._bucket)

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_raw(self, document_id: str, version: str, filename: str, content: bytes) -> str:
        key = raw_document_key(document_id, version, filename)
        logger.info(
            "Uploading document to gs://%s/%s (%d bytes)",
            self._bucket,
            key,
            len(content),
            extra={"document_id": document_id, "version": version, "gcs_key": key},
        )
        self._put_bytes(key, content, "application/octet-stream")
        return f"gs://{self._bucket}/{key}"

    # Backward-compatible alias
    def put_pdf(self, document_id: str, version: str, content: bytes, filename: str = "document.pdf") -> str:
        return self.put_raw(document_id, version, filename, content)

    def get_raw(self, document_id: str, version: str, filename: str) -> bytes:
        return self.get_bytes(raw_document_key(document_id, version, filename))

    def get_pdf(self, document_id: str, version: str) -> bytes:
        return self.get_bytes(raw_document_key(document_id, version, "source.pdf"))

    def put_json(self, key: str, payload: object) -> str:
        logger.debug("Writing JSON to gs://%s/%s", self._bucket, key, extra={"gcs_key": key})
        self._put_bytes(
            key,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        )
        return f"gs://{self._bucket}/{key}"

    @retry_on_transient()
    def get_json(self, key: str) -> object | None:
        logger.debug("Reading JSON from gs://%s/%s", self._bucket, key, extra={"gcs_key": key})
        try:
            data = self._bucket_obj.blob(key).download_as_bytes()
        except gcloud_exceptions.NotFound:
            logger.info("GCS JSON object not found: %s", key, extra={"gcs_key": key})
            return None
        return json.loads(data.decode("utf-8"))

    @retry_on_transient()
    def get_json_with_etag(self, key: str) -> tuple[object | None, str | None]:
        """Read a JSON object together with its GCS generation ("etag").

        Returns ``(payload, etag)`` on hit and ``(None, None)`` when the object
        does not exist. The etag (the object generation) feeds
        :meth:`put_json_conditional` for optimistic-concurrency writes.
        """
        logger.debug("Reading JSON+etag from gs://%s/%s", self._bucket, key, extra={"gcs_key": key})
        blob = self._bucket_obj.get_blob(key)
        if blob is None:
            return None, None
        data = blob.download_as_bytes()
        generation = blob.generation
        etag = str(generation) if generation is not None else None
        return json.loads(data.decode("utf-8")), etag

    def put_json_conditional(
        self,
        key: str,
        payload: object,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        """Write JSON only if the object's current state matches a precondition.

        - ``if_match``: succeed only if the object's current generation equals
          this value (i.e. it has not changed since we read it).
        - ``if_none_match``: succeed only if the object does not yet exist
          (create-only).

        Raises :class:`PreconditionFailed` (HTTP 412) when the precondition is
        not met, so the caller can reload and re-evaluate. Transient errors are
        retried, but a 412 is deterministic and must not be.
        """
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if if_none_match:
            generation = 0
        elif if_match is not None:
            generation = int(if_match)
        else:
            generation = None
        self._put_bytes_conditional(key, content, "application/json", generation)

    def create_json(self, key: str, payload: object) -> str:
        """Create a JSON object only if it does not already exist.

        This is the create-only (``if_none_match``) write used for immutable
        artifacts (document version manifests, ingestion events, clarification
        records, evaluation run results, AI configuration versions, corpus
        snapshots, SQL result fixtures, knowledge gap maps). A second write to
        the same key raises :class:`PreconditionFailed`, which is exactly the
        immutability guarantee those artifacts require.
        """
        self.put_json_conditional(key, payload, if_none_match=True)
        return f"gs://{self._bucket}/{key}"

    def update_json_cas(
        self,
        key: str,
        mutate: Callable[[object | None], object],
        *,
        max_attempts: int = 8,
    ) -> object:
        """Read-modify-write a JSON object under optimistic concurrency.

        Reads the current object with its generation, applies ``mutate`` to
        produce the next payload, and writes it conditionally (``if_none_match``
        when the object does not yet exist, ``if_match`` on the current
        generation otherwise). On a concurrent write (:class:`PreconditionFailed`)
        the read-modify-write is retried up to ``max_attempts`` times before
        giving up. ``mutate`` receives the current payload (or ``None`` when the
        object does not exist) and must return the full next payload; it may be
        called more than once and so must be free of side effects.

        This is the generation-CAS write used for contended, mutable artifacts
        (the document version index, feedback inbox index, evaluation sets, AI
        configuration index, and replay run state transitions).
        """
        for _ in range(max_attempts):
            current, etag = self.get_json_with_etag(key)
            payload = mutate(current)
            try:
                if current is None:
                    self.put_json_conditional(key, payload, if_none_match=True)
                else:
                    self.put_json_conditional(key, payload, if_match=etag)
                return payload
            except PreconditionFailed:
                # Another writer changed the object between our read and write;
                # reload and re-apply the mutation before retrying.
                continue
        raise PreconditionFailed(key)

    @retry_on_transient(exclude=PreconditionFailed)
    def _put_bytes_conditional(
        self, key: str, content: bytes, content_type: str, if_generation_match: int | None
    ) -> None:
        blob = self._bucket_obj.blob(key)
        if self._kms_key_name:
            blob.kms_key_name = self._kms_key_name
        try:
            blob.upload_from_string(
                content,
                content_type=content_type,
                if_generation_match=if_generation_match,
            )
        except gcloud_exceptions.PreconditionFailed as exc:
            logger.info(
                "Conditional GCS write rejected (precondition failed): %s",
                key,
                extra={"gcs_key": key},
            )
            raise PreconditionFailed(key) from exc
        logger.debug(
            "Conditional GCS put complete: %s (%d bytes)", key, len(content), extra={"gcs_key": key}
        )

    def put_chunks(self, document_id: str, version: str, chunks: Iterable[Chunk]) -> str:
        lines = [chunk.model_dump_json() for chunk in chunks]
        key = chunks_key(document_id, version)
        logger.info(
            "Writing %d chunks to gs://%s/%s",
            len(lines),
            self._bucket,
            key,
            extra={
                "document_id": document_id,
                "version": version,
                "gcs_key": key,
                "chunk_count": len(lines),
            },
        )
        self._put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"), "application/x-ndjson")
        return f"gs://{self._bucket}/{key}"

    @retry_on_transient()
    def get_bytes(self, key: str) -> bytes:
        logger.debug("Reading bytes from gs://%s/%s", self._bucket, key, extra={"gcs_key": key})
        return self._bucket_obj.blob(key).download_as_bytes()

    @retry_on_transient()
    def _put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        blob = self._bucket_obj.blob(key)
        if self._kms_key_name:
            blob.kms_key_name = self._kms_key_name
        blob.upload_from_string(content, content_type=content_type)
        logger.debug("GCS put complete: %s (%d bytes)", key, len(content), extra={"gcs_key": key})

    def _list_keys(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix``."""
        return [blob.name for blob in self._client.list_blobs(self._bucket_obj, prefix=prefix)]

    def list_document_record_keys(self) -> list[str]:
        """Return the object keys of all persisted document records."""
        return [key for key in self._list_keys("documents/") if key.endswith("/record.json")]

    def list_query_trace_keys(self) -> list[str]:
        """Return the object keys of all persisted query traces.

        Query traces live at ``queries/{trace_id}/trace.json``. Used by the
        knowledge-gap analysis to enumerate eligible outcomes without reaching
        into the underlying client's listing API.
        """
        return [key for key in self._list_keys("queries/") if key.endswith("/trace.json")]

    def list_feedback_record_keys(self) -> list[str]:
        """Return the object keys of all persisted query-feedback records.

        Feedback lives at ``queries/{trace_id}/feedback/{feedback_id}.json``.
        The operator inbox actions (classify/promote/resolve, R6) are addressed
        by ``feedback_id`` alone, so the service scans these keys to resolve the
        full key for a generation-CAS update.
        """
        return [
            key
            for key in self._list_keys("queries/")
            if "/feedback/" in key and key.endswith(".json")
        ]

    def list_ingestion_event_keys(self, document_id: str) -> list[str]:
        """Return the object keys of all Ingestion_Events for a Document (R5.7)."""
        prefix = f"documents/{document_id}/ingestions/"
        return [key for key in self._list_keys(prefix) if key.endswith(".json")]

    def list_corpus_snapshot_keys(self) -> list[str]:
        """Return the object keys of all persisted CorpusSnapshot records (R8.1)."""
        return [
            key
            for key in self._list_keys("corpus_snapshots/")
            # Only top-level snapshot files, not nested sql fixtures.
            if key.endswith(".json") and "/sql/" not in key
        ]

    def list_evaluation_run_keys(self) -> list[str]:
        """Return the object keys of all persisted evaluation-run result files (R7)."""
        return [
            key for key in self._list_keys("evaluation/runs/") if key.endswith("/results.json")
        ]

    def list_evaluation_set_case_keys(self, set_id: str) -> list[str]:
        """Return the object keys of all Benchmark_Cases in an Evaluation_Set (R7)."""
        prefix = f"evaluation/sets/{set_id}/cases/"
        return [key for key in self._list_keys(prefix) if key.endswith(".json")]

    @retry_on_transient()
    def get_chunks(self, document_id: str, version: str) -> list[Chunk]:
        """Read the retained chunks of a Document_Version (R5.5/R5.9).

        Returns the parsed ``Chunk`` list persisted by :meth:`put_chunks`, or an
        empty list when no chunks were retained for the version. Used by the
        restore path to re-index a version from its retained content when its
        vectors have been cleaned up.
        """
        key = chunks_key(document_id, version)
        try:
            body = self.get_bytes(key)
        except gcloud_exceptions.NotFound:
            return []
        chunks: list[Chunk] = []
        for line in body.decode("utf-8").splitlines():
            line = line.strip()
            if line:
                chunks.append(Chunk.model_validate_json(line))
        return chunks


def raw_document_key(document_id: str, version: str, filename: str) -> str:
    """Return the object key for a raw uploaded document, preserving original extension."""
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


def conversation_key(conversation_id: str) -> str:
    return f"conversations/{conversation_id}/conversation.json"


# --- Document version control (R5) ------------------------------------------


def document_version_key(document_id: str, version: str) -> str:
    """Immutable manifest for a single Document_Version (create-only)."""
    return f"documents/{document_id}/versions/{version}.json"


def document_version_index_key(document_id: str) -> str:
    """Ordered version list + active pointer for a Document (generation CAS)."""
    return f"documents/{document_id}/versions/index.json"


def ingestion_event_key(document_id: str, ingestion_id: str) -> str:
    """Immutable record of a single Ingestion_Event (create-only)."""
    return f"documents/{document_id}/ingestions/{ingestion_id}.json"


# --- Ambiguity clarification (R2) -------------------------------------------


def clarification_key(clarification_id: str) -> str:
    """Immutable Clarification_Record bound to a conversation turn (create-only)."""
    return f"clarifications/{clarification_id}.json"


# --- Feedback review inbox (R6) ---------------------------------------------


def feedback_index_key() -> str:
    """Append log indexing negative-rating Feedback_Items (generation CAS)."""
    return "feedback_index/negative.jsonl"


# --- Multi-method evaluation (R7) -------------------------------------------


def evaluation_set_case_key(set_id: str, case_id: str) -> str:
    """A Benchmark_Case within an Evaluation_Set (create / generation CAS)."""
    return f"evaluation/sets/{set_id}/cases/{case_id}.json"


def evaluation_run_results_key(run_id: str) -> str:
    """Immutable results of an evaluation run (create-only)."""
    return f"evaluation/runs/{run_id}/results.json"


# --- Versioned AI configuration (R9) ----------------------------------------


def ai_config_version_key(config_id: str, version_id: str) -> str:
    """Immutable AI_Configuration_Version (create-only)."""
    return f"ai_config/{config_id}/versions/{version_id}.json"


def ai_config_index_key(config_id: str) -> str:
    """History + active pointer + activation events for an AI config (generation CAS)."""
    return f"ai_config/{config_id}/index.json"


# --- Replay and compare lab (R8) --------------------------------------------


def corpus_snapshot_key(corpus_snapshot_id: str) -> str:
    """Immutable Corpus_Snapshot manifest (create-only)."""
    return f"corpus_snapshots/{corpus_snapshot_id}.json"


def sql_result_fixture_key(corpus_snapshot_id: str, fixture_id: str) -> str:
    """Immutable SQL_Result_Fixture for a Corpus_Snapshot (create-only)."""
    return f"corpus_snapshots/{corpus_snapshot_id}/sql/{fixture_id}.json"


def replay_run_key(replay_run_id: str) -> str:
    """Replay_Run record; state transitions written under generation CAS."""
    return f"replays/{replay_run_id}.json"


# --- Knowledge gap map (R11) ------------------------------------------------


def knowledge_gap_map_key(generated_at: str) -> str:
    """Immutable cached Knowledge_Gap_Map generation (create-only)."""
    return f"knowledge_gap/{generated_at}.json"


# Backward-compatibility alias. The artifact store migrated from AWS S3 to GCS;
# the historical name is retained so existing imports (and tests that bind the
# backend-agnostic ``create_json`` / ``update_json_cas`` helpers) keep working.
S3ArtifactStore = GcsArtifactStore
