# Bedrock/S3/SQS → GCP migration

This records the move of the data/AI plane off AWS onto GCP. The compute (the
Compute Engine VM) and text generation (Gemini on Vertex AI) were already on GCP;
this migration replaces the three remaining AWS services that were tied to the
AWS account being shut down.

| Concern | Before (AWS) | After (GCP) |
|---|---|---|
| Embeddings | Bedrock Titan `amazon.titan-embed-text-v2:0` (1024-dim) | Vertex AI `gemini-embedding-001` (3072-dim) |
| Vector index | Pinecone `dens-sparse-rag-prod` (1024, dotproduct) | Pinecone `dense-sparse-rag-gemini` (3072, dotproduct) |
| Artifact store | AWS S3 | Google Cloud Storage |
| Ingestion queue | AWS SQS | Google Cloud Pub/Sub |
| Reranker | Bedrock Cohere (disabled) | **unchanged** — still Bedrock, still off |
| Copilot / auth / traces DB | AWS RDS PostgreSQL | **unchanged** — different AWS account, not shut down |

## Code changes (already done)

- `config.py` — new settings: `RAG_GCS_BUCKET`, `RAG_GCS_KMS_KEY_NAME`,
  `RAG_PUBSUB_TOPIC_ID`, `RAG_PUBSUB_SUBSCRIPTION_ID`, `EMBEDDING_MODEL_ID`,
  `EMBEDDING_DIMENSION=3072`, `RAG_PINECONE_CLOUD/REGION/METRIC`. Added
  `gcs_client()`, `pubsub_publisher()`, `pubsub_subscriber()`. Kept the AWS/boto3
  helpers for the disabled Bedrock reranker.
- `embedding.py` — `GeminiEmbedder` (Vertex AI, `google-genai`), L2-normalized
  vectors, `RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY` task types, same bounded
  concurrent fan-out.
- `storage.py` — `GcsArtifactStore` (google-cloud-storage). ETag-CAS now uses GCS
  object *generations* (`if_generation_match`). `S3ArtifactStore` kept as a
  compatibility alias.
- `queue.py` — `PubSubIngestionQueue` (google-cloud-pubsub). `receipt_handle` →
  `ack_id`.
- `requirements.txt` / `pyproject.toml` — added `google-cloud-storage`,
  `google-cloud-pubsub`.

## GCP resources (provisioned)

These were created in project `project-619b14fd-4c6b-4f0a-b60`:

- **GCS bucket** `rag-console-artifacts-619b14fd` (location `us-east1`, uniform
  access) → `RAG_GCS_BUCKET=rag-console-artifacts-619b14fd`.
- **Pub/Sub topic** `rag-ingestion` and pull **subscription** `rag-ingestion-sub`
  (ack-deadline 600s) → `RAG_PUBSUB_TOPIC_ID` / `RAG_PUBSUB_SUBSCRIPTION_ID`.
- **IAM** on the VM service account
  `744448677871-compute@developer.gserviceaccount.com`: `roles/storage.objectAdmin`,
  `roles/pubsub.publisher`, `roles/pubsub.subscriber` (added to the existing
  `roles/aiplatform.user` + `cloud-platform` scope).

The commands used, for reference / reproduction:

### 1. GCS bucket

```bash
gcloud storage buckets create gs://rag-console-artifacts-619b14fd \
  --project=project-619b14fd-4c6b-4f0a-b60 \
  --location=us-east1 \
  --uniform-bucket-level-access
```

### 2. Pub/Sub topic + pull subscription

```bash
gcloud pubsub topics create rag-ingestion \
  --project=project-619b14fd-4c6b-4f0a-b60

# ack-deadline must exceed worst-case ingestion time (parse+embed+index).
gcloud pubsub subscriptions create rag-ingestion-sub \
  --topic=rag-ingestion \
  --ack-deadline=600 \
  --project=project-619b14fd-4c6b-4f0a-b60
```

### 3. IAM for the VM service account

```bash
SA=744448677871-compute@developer.gserviceaccount.com
PROJECT=project-619b14fd-4c6b-4f0a-b60

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin" --condition=None

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/pubsub.publisher" --condition=None

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/pubsub.subscriber" --condition=None
```

### 4. Pinecone index

Already created by `scripts/create_pinecone_index.py`:
`dense-sparse-rag-gemini` (3072-dim, dotproduct, serverless aws/us-east-1). The
`aws/us-east-1` placement is Pinecone-hosted infrastructure and is unrelated to
your own AWS account.

## Re-embedding note

Vectors from Titan and Gemini live in different spaces and have different
dimensions, so **the old Pinecone index is not reusable** — re-ingest existing
documents to populate `dense-sparse-rag-gemini`.

## Security

The previously committed AWS keys were blanked in `.env`. Deactivate/rotate them
in AWS (the account is being decommissioned). Consider moving the remaining
secrets (Pinecone, LlamaParse, DB, JWT) into GCP Secret Manager.
