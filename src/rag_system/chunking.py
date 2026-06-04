import hashlib
import re

from rag_system.config import Settings
from rag_system.models import Chunk, ParsedDocument
from rag_system.observability import get_logger

logger = get_logger(__name__)


class SemanticChunker:
    def __init__(self, settings: Settings):
        from llama_index.core.node_parser import SemanticSplitterNodeParser
        from llama_index.embeddings.bedrock import BedrockEmbedding

        session = settings.boto3_session()
        embed_model = BedrockEmbedding(
            model_name=settings.bedrock_embedding_model_id,
            client=session.client("bedrock-runtime"),
        )
        self._splitter = SemanticSplitterNodeParser(
            buffer_size=1,
            breakpoint_percentile_threshold=95,
            embed_model=embed_model,
        )

    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        from llama_index.core.schema import Document

        logger.info(
            "Chunking document %s (v=%s, %d chars)",
            parsed.document_id,
            parsed.version,
            len(parsed.markdown),
            extra={"document_id": parsed.document_id, "version": parsed.version},
        )

        document = Document(text=parsed.markdown, metadata=parsed.metadata)
        nodes = self._splitter.get_nodes_from_documents([document])
        chunks: list[Chunk] = []
        for index, node in enumerate(nodes):
            text = node.get_content().strip()
            if not text:
                continue
            page_start, page_end = infer_page_range(text)
            chunks.append(
                Chunk(
                    id=stable_chunk_id(parsed.document_id, parsed.version, index, text),
                    document_id=parsed.document_id,
                    version=parsed.version,
                    text=text,
                    page_start=page_start,
                    page_end=page_end,
                    section_path=infer_section_path(text),
                    metadata={
                        **parsed.metadata,
                        "chunk_index": index,
                        "token_estimate": max(1, len(text) // 4),
                    },
                )
            )

        logger.info(
            "Chunking complete: %d chunks from %d nodes",
            len(chunks),
            len(nodes),
            extra={
                "document_id": parsed.document_id,
                "version": parsed.version,
                "chunk_count": len(chunks),
            },
        )
        return chunks


def stable_chunk_id(document_id: str, version: str, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{document_id}:{version}:{index:06d}:{digest}"


def infer_page_range(text: str) -> tuple[int | None, int | None]:
    pages = [int(match) for match in re.findall(r"(?:page|p\.)\s+(\d+)", text, re.I)]
    if not pages:
        return None, None
    return min(pages), max(pages)


def infer_section_path(text: str) -> list[str]:
    headings = []
    for line in text.splitlines():
        if line.startswith("#"):
            headings.append(line.lstrip("#").strip())
    return headings[:4]
