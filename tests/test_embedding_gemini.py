"""Unit tests for the Gemini (Vertex AI) embedder.

The embedder is constructed via ``object.__new__`` so the tests never touch the
google-genai SDK or real credentials; a fake client/types stand in for the SDK
surface. These cover the migration-critical behaviours: L2 normalization,
retrieval task-type selection (document vs query), and dimension plumbing.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from rag_system.embedding import (
    _TASK_TYPE_DOCUMENT,
    _TASK_TYPE_QUERY,
    GeminiEmbedder,
    _l2_normalize,
)
from rag_system.models import Chunk


class _FakeConfig:
    def __init__(self, *, task_type: str, output_dimensionality: int) -> None:
        self.task_type = task_type
        self.output_dimensionality = output_dimensionality


class _FakeTypes:
    EmbedContentConfig = _FakeConfig


class _FakeModels:
    def __init__(self, values: list[float]) -> None:
        self._values = values
        self.calls: list[SimpleNamespace] = []

    def embed_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        return SimpleNamespace(embeddings=[SimpleNamespace(values=list(self._values))])


class _FakeClient:
    def __init__(self, values: list[float]) -> None:
        self.models = _FakeModels(values)


def _embedder(values: list[float], *, dimension: int = 2, max_workers: int = 1) -> GeminiEmbedder:
    emb = object.__new__(GeminiEmbedder)
    emb._client = _FakeClient(values)
    emb._types = _FakeTypes
    emb._model_id = "gemini-embedding-001"
    emb._dimension = dimension
    emb._max_workers = max_workers
    emb._executor = None
    return emb


def test_l2_normalize_scales_to_unit_length() -> None:
    assert _l2_normalize([3.0, 4.0]) == [0.6, 0.8]


def test_l2_normalize_leaves_zero_vector_unchanged() -> None:
    assert _l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_embed_query_normalizes_and_uses_query_task_type() -> None:
    emb = _embedder([3.0, 4.0])
    vector = emb.embed_query("hello")

    assert vector == [0.6, 0.8]
    assert math.isclose(math.sqrt(sum(c * c for c in vector)), 1.0)
    call = emb._client.models.calls[0]
    assert call.config.task_type == _TASK_TYPE_QUERY
    assert call.config.output_dimensionality == 2
    assert call.contents == "hello"


def test_embed_chunks_uses_document_task_type_for_every_chunk() -> None:
    emb = _embedder([1.0, 0.0])
    chunks = [
        Chunk(id=f"c{i}", document_id="d", version="v", text=f"chunk-{i}") for i in range(3)
    ]
    embedded = emb.embed_chunks(chunks)

    assert len(embedded) == 3
    assert all(e.chunk is c for e, c in zip(embedded, chunks))
    calls = emb._client.models.calls
    assert len(calls) == 3
    assert {call.config.task_type for call in calls} == {_TASK_TYPE_DOCUMENT}


def test_embed_chunks_empty_returns_empty() -> None:
    emb = _embedder([1.0, 0.0])
    assert emb.embed_chunks([]) == []
    assert emb._client.models.calls == []
