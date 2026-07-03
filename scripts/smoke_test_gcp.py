"""Smoke test for the GCP data plane: GCS artifact store + Pub/Sub queue.

Exercises ONLY Google Cloud Storage and Pub/Sub — it never touches the database.
It writes/reads/CAS-checks a throwaway blob and publishes/pulls/acks a throwaway
message, then cleans both up.

Usage (from repo root):
    python scripts/smoke_test_gcp.py

Auth: uses Application Default Credentials. Locally you may need
    gcloud auth application-default login
On the VM the attached service account is used automatically.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_system.config import get_settings  # noqa: E402
from rag_system.queue import IngestionJob, PubSubIngestionQueue  # noqa: E402
from rag_system.storage import GcsArtifactStore, PreconditionFailed  # noqa: E402


def _check_gcs(settings) -> bool:
    print(f"\n=== GCS: bucket '{settings.gcs_bucket}' ===")
    store = GcsArtifactStore(settings)
    key = f"smoke/{uuid.uuid4()}.json"
    ok = True
    try:
        uri = store.put_json(key, {"smoke": True, "ts": time.time()})
        print(f"  put_json      -> {uri}")

        payload = store.get_json(key)
        assert payload and payload.get("smoke") is True, payload
        print(f"  get_json      -> {payload}")

        payload2, etag = store.get_json_with_etag(key)
        print(f"  generation    -> {etag}")

        # create-only on an existing key must fail its precondition.
        try:
            store.create_json(key, {"smoke": "again"})
            print("  create_json (existing) -> ERROR: expected PreconditionFailed")
            ok = False
        except PreconditionFailed:
            print("  create_json (existing) -> PreconditionFailed (correct)")

        # CAS round-trip on a fresh key.
        cas_key = f"smoke/{uuid.uuid4()}.json"
        store.update_json_cas(cas_key, lambda cur: {"n": (cur or {}).get("n", 0) + 1})
        store.update_json_cas(cas_key, lambda cur: {"n": (cur or {}).get("n", 0) + 1})
        final, _ = store.get_json_with_etag(cas_key)
        assert final == {"n": 2}, final
        print(f"  update_json_cas x2 -> {final} (correct)")

        # cleanup
        store._bucket_obj.blob(key).delete()
        store._bucket_obj.blob(cas_key).delete()
        print("  cleanup       -> deleted test objects")
    except Exception as exc:  # noqa: BLE001
        print(f"  GCS FAILED: {type(exc).__name__}: {exc}")
        return False
    return ok


def _check_pubsub(settings) -> bool:
    print(
        f"\n=== Pub/Sub: topic '{settings.pubsub_topic_id}' / "
        f"sub '{settings.pubsub_subscription_id}' ==="
    )
    queue = PubSubIngestionQueue(settings)
    marker = str(uuid.uuid4())
    job = IngestionJob(
        document_id=f"smoke-{marker}",
        version="v1",
        filename="smoke.pdf",
        s3_uri=f"gs://{settings.gcs_bucket}/smoke/{marker}",
    )
    try:
        message_id = queue.enqueue(job)
        print(f"  enqueue       -> message_id={message_id}")

        # Pull with a few attempts to absorb delivery latency.
        received = None
        for attempt in range(10):
            batch = queue.receive()
            for msg in batch:
                if msg.job.document_id == job.document_id:
                    received = msg
                else:
                    # Not ours (shouldn't happen on a fresh topic); ack to drain.
                    queue.delete(msg)
            if received is not None:
                break
            time.sleep(1.0)

        if received is None:
            print("  receive       -> ERROR: message not delivered within timeout")
            return False
        print(f"  receive       -> ack_id={received.ack_id[:16]}... job={received.job.document_id}")

        queue.delete(received)
        print("  delete (ack)  -> acknowledged")
    except Exception as exc:  # noqa: BLE001
        print(f"  Pub/Sub FAILED: {type(exc).__name__}: {exc}")
        return False
    return True


def main() -> int:
    settings = get_settings()
    gcs_ok = _check_gcs(settings)
    pubsub_ok = _check_pubsub(settings)

    print("\n=== RESULT ===")
    print(f"  GCS     : {'PASS' if gcs_ok else 'FAIL'}")
    print(f"  Pub/Sub : {'PASS' if pubsub_ok else 'FAIL'}")
    return 0 if (gcs_ok and pubsub_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
