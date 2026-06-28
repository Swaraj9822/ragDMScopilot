"""One-off helper: upload a local PDF to S3 and enqueue it for ingestion."""

import asyncio
import sys
from pathlib import Path

# Ensure src/ is importable when run from the repo root.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_system.config import get_settings  # noqa: E402
from rag_system.service import RagService  # noqa: E402


async def main(pdf_path: str) -> None:
    settings = get_settings()
    service = RagService(settings)
    content = Path(pdf_path).read_bytes()
    record = await service.queue_document(Path(pdf_path).name, content)
    print(f"QUEUED id={record.id} version={record.version} s3_uri={record.s3_uri}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "diagn.pdf"
    asyncio.run(main(target))
