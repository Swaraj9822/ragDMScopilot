"""Create the Pinecone index for the Gemini-embedding RAG stack.

The index is created to match the Gemini embedding configuration:
- dimension = EMBEDDING_DIMENSION (3072 for gemini-embedding-001)
- metric    = RAG_PINECONE_METRIC (dotproduct — required for sparse+dense hybrid)

Serverless placement (RAG_PINECONE_CLOUD / RAG_PINECONE_REGION) is Pinecone-hosted
infrastructure and is unrelated to any AWS account of yours.

Usage (from the repo root):
    python scripts/create_pinecone_index.py

All values are read from Settings (i.e. the .env file). The script is idempotent:
if an index of the same name already exists it verifies the dimension/metric and
exits without recreating it.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pinecone import Pinecone, ServerlessSpec  # noqa: E402

from rag_system.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    name = settings.pinecone_index_name
    dimension = settings.embedding_dimension
    metric = settings.pinecone_metric
    cloud = settings.pinecone_cloud
    region = settings.pinecone_region

    pc = Pinecone(api_key=settings.pinecone_api_key)

    if _index_exists(pc, name):
        description = pc.describe_index(name)
        existing_dim = int(getattr(description, "dimension", 0) or 0)
        existing_metric = getattr(description, "metric", "?")
        print(
            f"Index '{name}' already exists "
            f"(dimension={existing_dim}, metric={existing_metric})."
        )
        if existing_dim != int(dimension):
            print(
                f"WARNING: existing dimension {existing_dim} != configured {dimension}. "
                "Delete and recreate the index, or fix EMBEDDING_DIMENSION.",
                file=sys.stderr,
            )
            return 1
        return 0

    print(
        f"Creating index '{name}' (dimension={dimension}, metric={metric}, "
        f"cloud={cloud}, region={region}) ..."
    )
    pc.create_index(
        name=name,
        dimension=dimension,
        metric=metric,
        spec=ServerlessSpec(cloud=cloud, region=region),
    )
    print(f"Index '{name}' created.")
    return 0


def _index_exists(pc: "Pinecone", name: str) -> bool:
    """Version-tolerant existence check across Pinecone SDK releases."""
    has_index = getattr(pc, "has_index", None)
    if callable(has_index):
        return bool(has_index(name))
    listing = pc.list_indexes()
    names = getattr(listing, "names", None)
    if callable(names):
        return name in names()
    return any(getattr(index, "name", index) == name for index in listing)


if __name__ == "__main__":
    raise SystemExit(main())
