"""Tests for reranker: Settings validation and property-based tests."""

from __future__ import annotations

import math
import threading
import time as time_module
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from rag_system.config import Settings
from rag_system.models import Chunk, QueryRequest, QueryResponse, RankedHit, RetrievalHit
from rag_system.reranker import LLMReranker, RerankerProvider
from rag_system.service import RagService

# Base dict with all required fields set to valid values so we can test
# reranker fields in isolation without triggering unrelated validation errors.
_BASE = {
    "RAG_S3_BUCKET": "test-bucket",
    "RAG_INGESTION_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue",
    "LLAMA_CLOUD_API_KEY": "fake-key",
    "PINECONE_API_KEY": "fake-key",
    "PINECONE_INDEX_NAME": "test-index",
    "SECRETS_MANAGER_SECRET_ID": "",
}


def _make_settings(**overrides):
    """Create a Settings instance with required fields and optional overrides."""
    data = {**_BASE, **overrides}
    return Settings(**data)


# ---------------------------------------------------------------------------
# Requirement 4.1–4.6: Default values
# ---------------------------------------------------------------------------


class TestRerankerDefaults:
    """Verify all reranker fields have the expected default values."""

    def test_reranker_defaults(self):
        settings = _make_settings()
        assert settings.reranker_enabled is False
        assert settings.reranker_model_id == "gemini-3.5-flash"
        assert settings.reranker_top_k == 10
        assert settings.reranker_score_threshold is None
        assert settings.reranker_max_concurrent == 5
        assert settings.reranker_timeout_s == 30


# ---------------------------------------------------------------------------
# Requirement 4.3: reranker_top_k (1–100)
# ---------------------------------------------------------------------------


class TestRerankerTopK:
    def test_reranker_top_k_valid_boundaries(self):
        s_low = _make_settings(RAG_RERANKER_TOP_K=1)
        assert s_low.reranker_top_k == 1

        s_high = _make_settings(RAG_RERANKER_TOP_K=100)
        assert s_high.reranker_top_k == 100

    def test_reranker_top_k_invalid(self):
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_TOP_K=0)
        assert "reranker_top_k" in str(exc_info.value).lower() or "RAG_RERANKER_TOP_K" in str(
            exc_info.value
        )

        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_TOP_K=101)
        assert "reranker_top_k" in str(exc_info.value).lower() or "RAG_RERANKER_TOP_K" in str(
            exc_info.value
        )


# ---------------------------------------------------------------------------
# Requirement 4.4: reranker_score_threshold (0.0–1.0 when set)
# ---------------------------------------------------------------------------


class TestRerankerScoreThreshold:
    def test_reranker_score_threshold_valid(self):
        s_zero = _make_settings(RAG_RERANKER_SCORE_THRESHOLD=0.0)
        assert s_zero.reranker_score_threshold == 0.0

        s_one = _make_settings(RAG_RERANKER_SCORE_THRESHOLD=1.0)
        assert s_one.reranker_score_threshold == 1.0

    def test_reranker_score_threshold_invalid(self):
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_SCORE_THRESHOLD=-0.1)
        assert "reranker_score_threshold" in str(
            exc_info.value
        ).lower() or "RAG_RERANKER_SCORE_THRESHOLD" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_SCORE_THRESHOLD=1.1)
        assert "reranker_score_threshold" in str(
            exc_info.value
        ).lower() or "RAG_RERANKER_SCORE_THRESHOLD" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Requirement 4.5: reranker_max_concurrent (1–50)
# ---------------------------------------------------------------------------


class TestRerankerMaxConcurrent:
    def test_reranker_max_concurrent_valid(self):
        s_low = _make_settings(RAG_RERANKER_MAX_CONCURRENT=1)
        assert s_low.reranker_max_concurrent == 1

        s_high = _make_settings(RAG_RERANKER_MAX_CONCURRENT=50)
        assert s_high.reranker_max_concurrent == 50

    def test_reranker_max_concurrent_invalid(self):
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_MAX_CONCURRENT=0)
        assert "reranker_max_concurrent" in str(
            exc_info.value
        ).lower() or "RAG_RERANKER_MAX_CONCURRENT" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_MAX_CONCURRENT=51)
        assert "reranker_max_concurrent" in str(
            exc_info.value
        ).lower() or "RAG_RERANKER_MAX_CONCURRENT" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Requirement 4.6: reranker_timeout_s (1–300)
# ---------------------------------------------------------------------------


class TestRerankerTimeout:
    def test_reranker_timeout_valid(self):
        s_low = _make_settings(RAG_RERANKER_TIMEOUT_S=1)
        assert s_low.reranker_timeout_s == 1

        s_high = _make_settings(RAG_RERANKER_TIMEOUT_S=300)
        assert s_high.reranker_timeout_s == 300

    def test_reranker_timeout_invalid(self):
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_TIMEOUT_S=0)
        assert "reranker_timeout_s" in str(
            exc_info.value
        ).lower() or "RAG_RERANKER_TIMEOUT_S" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_TIMEOUT_S=301)
        assert "reranker_timeout_s" in str(
            exc_info.value
        ).lower() or "RAG_RERANKER_TIMEOUT_S" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Requirement 4.2: reranker_model_id max_length=128
# ---------------------------------------------------------------------------


class TestRerankerModelId:
    def test_reranker_model_id_max_length(self):
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(RAG_RERANKER_MODEL_ID="x" * 129)
        assert "reranker_model_id" in str(exc_info.value).lower() or "RAG_RERANKER_MODEL_ID" in str(
            exc_info.value
        )


# ---------------------------------------------------------------------------
# Property 5: Failure Yields Zero Score (Requirements 5.2, 5.3)
# Feature: llm-reranker, Property 5: Failure Yields Zero Score
# **Validates: Requirements 5.2, 5.3**
# ---------------------------------------------------------------------------


def _is_valid_score(s: str) -> bool:
    """Check if a string is a valid float in [0.0, 1.0]."""
    try:
        v = float(s.strip())
        return 0.0 <= v <= 1.0
    except (ValueError, TypeError):
        return False


@given(raw_text=st.text().filter(lambda s: not _is_valid_score(s)))
@hyp_settings(max_examples=100)
def test_property_failure_yields_zero_score(raw_text):
    """Non-parseable LLM responses always yield rerank_score of 0.0.

    **Validates: Requirements 5.2, 5.3**
    """
    provider = object.__new__(RerankerProvider)
    score = provider._parse_score(raw_text)
    assert score == 0.0


@given(
    value=st.one_of(
        st.floats(min_value=1.01, max_value=1000.0),
        st.floats(min_value=-1000.0, max_value=-0.01),
    )
)
@hyp_settings(max_examples=100)
def test_property_out_of_range_yields_zero(value):
    """Out-of-range float responses yield 0.0.

    **Validates: Requirements 5.2, 5.3**
    """
    provider = object.__new__(RerankerProvider)
    score = provider._parse_score(str(value))
    assert score == 0.0


# ---------------------------------------------------------------------------
# Property 3: Output Size, Ordering, and Threshold Filtering
# Feature: llm-reranker, Property 3: Output Size, Ordering, and Threshold Filtering
# **Validates: Requirements 1.1, 2.1, 2.2, 2.3**
# ---------------------------------------------------------------------------

# Strategies for generating test data for Property 3
_chunk_strategy = st.builds(
    Chunk,
    id=st.uuids().map(str),
    document_id=st.uuids().map(str),
    version=st.just("1.0"),
    text=st.text(min_size=1, max_size=200),
)

_hit_strategy = st.builds(
    RetrievalHit,
    chunk=_chunk_strategy,
    score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    source=st.just("pinecone"),
)


@given(
    hits=st.lists(_hit_strategy, min_size=1, max_size=20),
    top_k=st.integers(min_value=1, max_value=100),
    threshold=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
    scores=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=20, max_size=20
    ),
)
@hyp_settings(max_examples=100)
def test_property_output_size_ordering_threshold(hits, top_k, threshold, scores):
    """Property 3: Output satisfies no chunk below threshold, correct size, sorted descending.

    **Validates: Requirements 1.1, 2.1, 2.2, 2.3**
    """
    settings = _make_settings(
        RAG_RERANKER_ENABLED=True,
        RAG_RERANKER_TOP_K=top_k,
        RAG_RERANKER_SCORE_THRESHOLD=threshold,
    )
    reranker = LLMReranker(settings)

    # Create an iterator of scores to assign to each chunk
    score_iter = iter(scores)

    def mock_score(query, chunk_text):
        return next(score_iter)

    with patch.object(reranker._provider, "score_chunk", side_effect=mock_score):
        result = reranker.rerank("test query", hits)

    # Property: no chunk below threshold
    if threshold is not None:
        for rh in result:
            assert rh.rerank_score >= threshold, (
                f"Chunk has rerank_score {rh.rerank_score} below threshold {threshold}"
            )

    # Property: sorted descending by rerank_score
    for i in range(len(result) - 1):
        assert result[i].rerank_score >= result[i + 1].rerank_score, (
            f"Output not sorted descending: index {i} has {result[i].rerank_score} "
            f"< index {i + 1} has {result[i + 1].rerank_score}"
        )

    # Property: correct output size = min(top_k, count of chunks passing threshold)
    assigned_scores = scores[: len(hits)]
    if threshold is not None:
        passing = sum(1 for s in assigned_scores if s >= threshold)
    else:
        passing = len(hits)
    expected_size = min(top_k, passing)
    assert len(result) == expected_size, (
        f"Expected output size {expected_size} (top_k={top_k}, passing={passing}), "
        f"got {len(result)}"
    )


# ---------------------------------------------------------------------------
# Property 1: Score Bound Invariant (Requirement 1.2)
# Feature: llm-reranker, Property 1: Score Bound Invariant
# **Validates: Requirements 1.2**
# ---------------------------------------------------------------------------


@given(st.text())
@hyp_settings(max_examples=100)
def test_property_score_bound_invariant(raw_text: str) -> None:
    """Every rerank_score from _parse_score is in [0.0, 1.0] inclusive.

    **Validates: Requirements 1.2**
    """
    provider = object.__new__(RerankerProvider)
    score = provider._parse_score(raw_text)
    assert 0.0 <= score <= 1.0, f"Score {score!r} out of [0.0, 1.0] for input {raw_text!r}"


@given(st.floats(allow_nan=True, allow_infinity=True))
@hyp_settings(max_examples=100)
def test_property_score_bound_with_floats(value: float) -> None:
    """Score bound holds even with NaN, inf, or out-of-range float strings.

    **Validates: Requirements 1.2**
    """
    provider = object.__new__(RerankerProvider)
    raw_text = str(value)
    score = provider._parse_score(raw_text)
    assert 0.0 <= score <= 1.0, f"Score {score!r} out of [0.0, 1.0] for input {raw_text!r}"
    assert not math.isnan(score), f"Score is NaN for input {raw_text!r}"


# ---------------------------------------------------------------------------
# Property 2: Data Preservation (Requirement 1.4)
# Feature: llm-reranker, Property 2: Data Preservation
# **Validates: Requirements 1.4**
# ---------------------------------------------------------------------------


class TestPropertyDataPreservation:
    """Every output RankedHit preserves chunk, score, and source from the corresponding input.

    **Validates: Requirements 1.4**
    """

    @given(hits=st.lists(_hit_strategy, min_size=1, max_size=10))
    @hyp_settings(max_examples=100)
    def test_property_data_preservation(self, hits: list[RetrievalHit]):
        """For any list of RetrievalHit objects, every output RankedHit preserves
        chunk, score, and source from the corresponding input RetrievalHit."""
        settings = _make_settings(
            RAG_RERANKER_ENABLED=True,
            RAG_RERANKER_TOP_K=100,
            RAG_RERANKER_SCORE_THRESHOLD=None,
        )
        reranker = LLMReranker(settings)

        # Mock score_chunk to return a valid fixed score so no filtering occurs
        with patch.object(reranker._provider, "score_chunk", return_value=0.5):
            result = reranker.rerank("test query", hits)

        # With no threshold and top_k >= input size, all hits should appear in output
        assert len(result) == len(hits)

        # Build lookup from chunk.id to original hit
        input_by_id = {h.chunk.id: h for h in hits}

        for ranked in result:
            assert ranked.chunk.id in input_by_id, (
                f"Output chunk id {ranked.chunk.id} not found in input"
            )
            original = input_by_id[ranked.chunk.id]
            # Verify all original data is preserved
            assert ranked.chunk == original.chunk
            assert ranked.score == original.score
            assert ranked.source == original.source


# ---------------------------------------------------------------------------
# Property 7: Pointwise Scoring — One Call Per Chunk (Requirement 7.2)
# Feature: llm-reranker, Property 7: Pointwise Scoring — One Call Per Chunk
# **Validates: Requirements 7.2**
# ---------------------------------------------------------------------------


@given(num_hits=st.integers(min_value=1, max_value=50))
@hyp_settings(max_examples=100)
def test_property_pointwise_one_call_per_chunk(num_hits):
    """Exactly N LLM calls are made for N input chunks.

    **Validates: Requirements 7.2**
    """
    settings = _make_settings(
        RAG_RERANKER_ENABLED=True,
        RAG_RERANKER_TOP_K=100,
        RAG_RERANKER_SCORE_THRESHOLD=None,
    )
    reranker = LLMReranker(settings)
    call_count = [0]

    def mock_score(query, chunk_text):
        call_count[0] += 1
        return 0.5

    # Generate hits
    hits = [
        RetrievalHit(
            chunk=Chunk(id=str(i), document_id="doc1", version="1.0", text=f"chunk {i}"),
            score=0.8,
            source="pinecone",
        )
        for i in range(num_hits)
    ]

    with patch.object(reranker._provider, "score_chunk", side_effect=mock_score):
        reranker.rerank("test query", hits)

    assert call_count[0] == num_hits, f"Expected {num_hits} LLM calls, got {call_count[0]}"


# ---------------------------------------------------------------------------
# Property 6: Concurrency Bound (Requirement 7.1)
# Feature: llm-reranker, Property 6: Concurrency Bound
# **Validates: Requirements 7.1**
# ---------------------------------------------------------------------------


@given(
    num_hits=st.integers(min_value=5, max_value=30),
    max_concurrent=st.integers(min_value=1, max_value=5),
)
@hyp_settings(max_examples=100, deadline=None)
def test_property_concurrency_bound(num_hits, max_concurrent):
    """Peak in-flight LLM calls never exceeds reranker_max_concurrent.

    **Validates: Requirements 7.1**
    """
    settings = _make_settings(
        RAG_RERANKER_ENABLED=True,
        RAG_RERANKER_TOP_K=100,
        RAG_RERANKER_SCORE_THRESHOLD=None,
        RAG_RERANKER_MAX_CONCURRENT=max_concurrent,
    )
    reranker = LLMReranker(settings)

    # Track peak concurrency
    counter_lock = threading.Lock()
    peak = [0]
    active = [0]

    def mock_score(query, chunk_text):
        with counter_lock:
            active[0] += 1
            if active[0] > peak[0]:
                peak[0] = active[0]
        time_module.sleep(0.01)  # Small delay to create concurrency overlap
        with counter_lock:
            active[0] -= 1
        return 0.5

    # Generate hits
    hits = [
        RetrievalHit(
            chunk=Chunk(id=str(i), document_id="doc1", version="1.0", text=f"chunk {i}"),
            score=0.8,
            source="pinecone",
        )
        for i in range(num_hits)
    ]

    with patch.object(reranker._provider, "score_chunk", side_effect=mock_score):
        reranker.rerank("test query", hits)

    assert peak[0] <= max_concurrent, (
        f"Peak concurrency {peak[0]} exceeds max_concurrent {max_concurrent}"
    )


# ---------------------------------------------------------------------------
# Property 8: Fault Isolation Within Batch (Requirement 7.4)
# Feature: llm-reranker, Property 8: Fault Isolation Within Batch
# **Validates: Requirements 7.4**
# ---------------------------------------------------------------------------


@given(
    num_hits=st.integers(min_value=2, max_value=20),
    failure_indices=st.lists(st.integers(min_value=0, max_value=19), max_size=10),
)
@hyp_settings(max_examples=100)
def test_property_fault_isolation(num_hits, failure_indices):
    """All non-failed calls in a batch complete successfully with valid scores.

    **Validates: Requirements 7.4**
    """
    settings = _make_settings(
        RAG_RERANKER_ENABLED=True,
        RAG_RERANKER_TOP_K=100,
        RAG_RERANKER_SCORE_THRESHOLD=None,
    )
    reranker = LLMReranker(settings)

    # Normalize failure_indices to be within range
    fail_set = {i % num_hits for i in failure_indices}

    def mock_score(query, chunk_text):
        # Extract chunk index from text "chunk N"
        idx = int(chunk_text.split()[-1])
        if idx in fail_set:
            raise RuntimeError(f"Simulated failure for chunk {idx}")
        return 0.7

    hits = [
        RetrievalHit(
            chunk=Chunk(id=str(i), document_id="doc1", version="1.0", text=f"chunk {i}"),
            score=0.8,
            source="pinecone",
        )
        for i in range(num_hits)
    ]

    with patch.object(reranker._provider, "score_chunk", side_effect=mock_score):
        result = reranker.rerank("test query", hits)

    # All results should have valid scores
    assert len(result) == num_hits  # all chunks should be in output (failed ones get 0.0)
    for rh in result:
        assert 0.0 <= rh.rerank_score <= 1.0

    # Non-failed chunks should have score 0.7
    # Failed chunks should have score 0.0
    successful_count = sum(1 for rh in result if rh.rerank_score == 0.7)
    failed_count = sum(1 for rh in result if rh.rerank_score == 0.0)
    assert successful_count == num_hits - len(fail_set)
    assert failed_count == len(fail_set)


# ---------------------------------------------------------------------------
# Property 4: Graceful Fallback Preserves Original Hits (Requirement 3.4)
# Feature: llm-reranker, Property 4: Graceful Fallback Preserves Original Hits
# **Validates: Requirements 3.4**
# ---------------------------------------------------------------------------


@given(
    hits=st.lists(_hit_strategy, min_size=1, max_size=10),
    error_type=st.sampled_from([RuntimeError, TimeoutError, ValueError]),
)
@hyp_settings(max_examples=100)
def test_property_graceful_fallback(hits, error_type):
    """When reranker raises an exception, RagService passes original hits to generator unchanged.

    **Validates: Requirements 3.4**
    """
    settings = _make_settings(
        RAG_RERANKER_ENABLED=True,
        RAG_RERANKER_TOP_K=10,
    )

    # Build a RagService with mocked dependencies (bypass __init__)
    service = object.__new__(RagService)
    service._settings = settings
    service._init_lock = threading.Lock()

    # Mock embedder to return a dummy vector
    mock_embedder = MagicMock()
    mock_embedder.embed_query.return_value = [0.1] * 1024
    service._embedder = mock_embedder

    # Mock sparse encoder
    service._sparse_encoder = MagicMock()
    service._sparse_encoder.encode_query.return_value = {"indices": [1, 2], "values": [0.5, 0.3]}

    # Mock index to return our generated hits
    mock_index = MagicMock()
    mock_index.search.return_value = hits
    service._index = mock_index

    # Mock generator - capture what hits it receives
    received_hits = []
    mock_generator = MagicMock()

    def capture_answer(question, gen_hits, trace_id):
        received_hits.extend(gen_hits)
        return QueryResponse(
            answer="test answer",
            citations=[],
            evidence_status="grounded",
            trace_id=trace_id,
        )

    mock_generator.answer.side_effect = capture_answer
    service._generator = mock_generator

    # Mock reranker to raise the parameterized exception type
    mock_reranker = MagicMock()
    mock_reranker.rerank.side_effect = error_type("simulated failure")
    service._reranker = mock_reranker

    # Mock observability methods to avoid side effects
    service._observe_retrieval_quality = MagicMock()
    service._observe_answer_quality = MagicMock()

    # Execute query
    request = QueryRequest(question="What is the answer?")
    service.query(request)

    # Assert generator received the ORIGINAL unreranked hits unchanged
    assert len(received_hits) == len(hits), (
        f"Expected {len(hits)} hits passed to generator, got {len(received_hits)}"
    )
    for original, received in zip(hits, received_hits):
        assert received.chunk == original.chunk, (
            f"Chunk content mismatch: expected {original.chunk}, got {received.chunk}"
        )
        assert received.score == original.score, (
            f"Score mismatch: expected {original.score}, got {received.score}"
        )
        assert received.source == original.source, (
            f"Source mismatch: expected {original.source}, got {received.source}"
        )


# ---------------------------------------------------------------------------
# Task 5.3: Pipeline Integration Unit Tests
# Requirements: 3.1, 3.2, 3.3, 3.4, 5.4, 5.6
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    """Unit tests for reranker pipeline integration in RagService.query().

    Requirements: 3.1, 3.2, 3.3, 3.4, 5.4, 5.6
    """

    def _build_service(self, reranker_enabled=True, hits=None):
        """Build a RagService with all dependencies mocked."""
        settings = _make_settings(
            RAG_RERANKER_ENABLED=reranker_enabled,
            RAG_RERANKER_TOP_K=10,
        )
        service = object.__new__(RagService)
        service._settings = settings
        service._init_lock = threading.Lock()

        # Default hits if none provided
        if hits is None:
            hits = [
                RetrievalHit(
                    chunk=Chunk(id="c1", document_id="d1", version="1.0", text="chunk 1"),
                    score=0.9,
                    source="pinecone",
                ),
                RetrievalHit(
                    chunk=Chunk(id="c2", document_id="d1", version="1.0", text="chunk 2"),
                    score=0.7,
                    source="pinecone",
                ),
            ]

        # Mock embedder
        mock_embedder = MagicMock()
        mock_embedder.embed_query.return_value = [0.1] * 1024
        service._embedder = mock_embedder

        # Mock sparse encoder
        service._sparse_encoder = MagicMock()
        service._sparse_encoder.encode_query.return_value = {"indices": [1], "values": [0.5]}

        # Mock index
        mock_index = MagicMock()
        mock_index.search.return_value = hits
        service._index = mock_index

        # Mock generator - capture what hits it receives
        service._received_hits = []
        mock_generator = MagicMock()

        def capture_answer(question, gen_hits, trace_id):
            service._received_hits = gen_hits
            return QueryResponse(
                answer="ans", citations=[], evidence_status="grounded", trace_id=trace_id
            )

        mock_generator.answer.side_effect = capture_answer
        service._generator = mock_generator

        # Mock observation methods
        service._observe_retrieval_quality = MagicMock()
        service._observe_answer_quality = MagicMock()

        return service, hits

    def test_reranker_invoked_when_enabled(self):
        """Reranker is called when enabled and results passed to generator (Req 3.1)."""
        service, hits = self._build_service(reranker_enabled=True)

        # Mock reranker to return reranked results
        mock_reranker = MagicMock()
        ranked = [
            RankedHit(chunk=h.chunk, score=h.score, source=h.source, rerank_score=0.9 - i * 0.1)
            for i, h in enumerate(hits)
        ]
        mock_reranker.rerank.return_value = ranked
        service._reranker = mock_reranker

        # Mock circuit breaker to allow the request
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True
        with patch("rag_system.service.get_circuit_breaker", return_value=mock_cb):
            service.query(QueryRequest(question="test?"))

        mock_reranker.rerank.assert_called_once()
        # Generator should receive reranked hits (converted to RetrievalHit)
        assert len(service._received_hits) == len(ranked)

    def test_reranker_skipped_when_disabled(self):
        """Reranker is NOT called when disabled (Req 3.2)."""
        service, hits = self._build_service(reranker_enabled=False)

        mock_reranker = MagicMock()
        service._reranker = mock_reranker

        service.query(QueryRequest(question="test?"))

        mock_reranker.rerank.assert_not_called()
        # Generator receives original hits unchanged
        assert len(service._received_hits) == len(hits)

    def test_fallback_on_error(self):
        """On reranker error, original hits pass through to generator (Req 3.4)."""
        service, hits = self._build_service(reranker_enabled=True)

        mock_reranker = MagicMock()
        mock_reranker.rerank.side_effect = RuntimeError("LLM down")
        service._reranker = mock_reranker

        # Mock circuit breaker to allow the request
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True
        with patch("rag_system.service.get_circuit_breaker", return_value=mock_cb):
            service.query(QueryRequest(question="test?"))

        # Generator should receive original hits unchanged
        assert service._received_hits == hits

    def test_fallback_on_timeout(self):
        """On reranker timeout, original hits pass through to generator (Req 5.4)."""
        service, hits = self._build_service(reranker_enabled=True)

        mock_reranker = MagicMock()
        mock_reranker.rerank.side_effect = TimeoutError("took too long")
        service._reranker = mock_reranker

        # Mock circuit breaker to allow the request
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True
        with patch("rag_system.service.get_circuit_breaker", return_value=mock_cb):
            service.query(QueryRequest(question="test?"))

        assert service._received_hits == hits

    def test_fallback_on_circuit_breaker_open(self):
        """When circuit breaker is open, reranker is skipped entirely (Req 5.6)."""
        service, hits = self._build_service(reranker_enabled=True)

        mock_reranker = MagicMock()
        service._reranker = mock_reranker

        # Mock circuit breaker to reject the request (open state)
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = False
        with patch("rag_system.service.get_circuit_breaker", return_value=mock_cb):
            service.query(QueryRequest(question="test?"))

        # Reranker should NOT have been called
        mock_reranker.rerank.assert_not_called()
        # Generator receives original hits unchanged
        assert service._received_hits == hits


# ---------------------------------------------------------------------------
# Task 6.2: Observability Unit Tests
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
# ---------------------------------------------------------------------------


class TestObservability:
    """Verify reranker observability instrumentation.

    Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
    """

    def _make_reranker_and_hits(self, num_hits=3):
        """Create a configured LLMReranker and sample hits."""
        settings = _make_settings(
            RAG_RERANKER_ENABLED=True,
            RAG_RERANKER_TOP_K=10,
            RAG_RERANKER_SCORE_THRESHOLD=None,
        )
        reranker = LLMReranker(settings)
        hits = [
            RetrievalHit(
                chunk=Chunk(id=str(i), document_id="doc1", version="1.0", text=f"chunk {i}"),
                score=0.8,
                source="pinecone",
            )
            for i in range(num_hits)
        ]
        return reranker, hits

    def test_reranker_duration_metric_emitted(self):
        """rag_reranker_duration_ms metric is emitted on successful rerank (Req 6.2)."""
        from rag_system.observability import metrics

        reranker, hits = self._make_reranker_and_hits()
        with patch.object(reranker._provider, "score_chunk", return_value=0.8):
            with patch.object(metrics, "observe") as mock_observe:
                reranker.rerank("test query", hits)

        # Check rag_reranker_duration_ms was emitted
        duration_calls = [
            c for c in mock_observe.call_args_list if c[0][0] == "rag_reranker_duration_ms"
        ]
        assert len(duration_calls) == 1
        assert duration_calls[0][0][1] > 0  # duration should be positive

    def test_reranker_input_count_metric(self):
        """rag_reranker_input_count emitted with number of input chunks (Req 6.3)."""
        from rag_system.observability import metrics

        reranker, hits = self._make_reranker_and_hits(num_hits=5)
        with patch.object(reranker._provider, "score_chunk", return_value=0.7):
            with patch.object(metrics, "observe") as mock_observe:
                reranker.rerank("test query", hits)

        input_calls = [
            c for c in mock_observe.call_args_list if c[0][0] == "rag_reranker_input_count"
        ]
        assert len(input_calls) == 1
        assert input_calls[0][0][1] == 5.0

    def test_reranker_output_count_metric(self):
        """rag_reranker_output_count emitted with number of output chunks (Req 6.4)."""
        from rag_system.observability import metrics

        reranker, hits = self._make_reranker_and_hits(num_hits=5)
        with patch.object(reranker._provider, "score_chunk", return_value=0.7):
            with patch.object(metrics, "observe") as mock_observe:
                reranker.rerank("test query", hits)

        output_calls = [
            c for c in mock_observe.call_args_list if c[0][0] == "rag_reranker_output_count"
        ]
        assert len(output_calls) == 1
        assert output_calls[0][0][1] == 5.0  # all pass with no threshold

    def test_reranker_top_score_metric(self):
        """rag_reranker_top_score metric emitted on success (Req 6.7)."""
        from rag_system.observability import metrics

        reranker, hits = self._make_reranker_and_hits(num_hits=3)

        score_values = iter([0.9, 0.5, 0.3])
        with patch.object(
            reranker._provider, "score_chunk", side_effect=lambda q, t: next(score_values)
        ):
            with patch.object(metrics, "observe") as mock_observe:
                reranker.rerank("test query", hits)

        top_calls = [c for c in mock_observe.call_args_list if c[0][0] == "rag_reranker_top_score"]
        assert len(top_calls) == 1
        assert top_calls[0][0][1] == 0.9

    def test_fallback_counter_on_timeout(self):
        """rag_reranker_fallback_total counter with reason=timeout (Req 6.5)."""
        from rag_system.observability import metrics

        settings = _make_settings(RAG_RERANKER_ENABLED=True, RAG_RERANKER_TOP_K=10)
        service = object.__new__(RagService)
        service._settings = settings
        service._init_lock = threading.Lock()

        mock_embedder = MagicMock()
        mock_embedder.embed_query.return_value = [0.1] * 1024
        service._embedder = mock_embedder
        service._sparse_encoder = MagicMock()
        service._sparse_encoder.encode_query.return_value = {"indices": [1], "values": [0.5]}

        mock_index = MagicMock()
        hit = RetrievalHit(
            chunk=Chunk(id="c1", document_id="d1", version="1.0", text="t"),
            score=0.9,
            source="p",
        )
        mock_index.search.return_value = [hit]
        service._index = mock_index

        mock_generator = MagicMock()
        mock_generator.answer.return_value = QueryResponse(
            answer="a", citations=[], evidence_status="g", trace_id="t"
        )
        service._generator = mock_generator
        service._observe_retrieval_quality = MagicMock()
        service._observe_answer_quality = MagicMock()

        mock_reranker = MagicMock()
        mock_reranker.rerank.side_effect = TimeoutError("timeout")
        service._reranker = mock_reranker

        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        with patch("rag_system.service.get_circuit_breaker", return_value=mock_cb):
            with patch.object(metrics, "increment") as mock_increment:
                service.query(QueryRequest(question="test?"))

        # Check fallback counter was incremented with reason=timeout
        timeout_calls = [
            c
            for c in mock_increment.call_args_list
            if c[0][0] == "rag_reranker_fallback_total" and c[0][1].get("reason") == "timeout"
        ]
        assert len(timeout_calls) == 1

    def test_fallback_counter_on_error(self):
        """rag_reranker_fallback_total counter with reason=error (Req 6.5)."""
        from rag_system.observability import metrics

        settings = _make_settings(RAG_RERANKER_ENABLED=True, RAG_RERANKER_TOP_K=10)
        service = object.__new__(RagService)
        service._settings = settings
        service._init_lock = threading.Lock()

        mock_embedder = MagicMock()
        mock_embedder.embed_query.return_value = [0.1] * 1024
        service._embedder = mock_embedder
        service._sparse_encoder = MagicMock()
        service._sparse_encoder.encode_query.return_value = {"indices": [1], "values": [0.5]}

        mock_index = MagicMock()
        hit = RetrievalHit(
            chunk=Chunk(id="c1", document_id="d1", version="1.0", text="t"),
            score=0.9,
            source="p",
        )
        mock_index.search.return_value = [hit]
        service._index = mock_index

        mock_generator = MagicMock()
        mock_generator.answer.return_value = QueryResponse(
            answer="a", citations=[], evidence_status="g", trace_id="t"
        )
        service._generator = mock_generator
        service._observe_retrieval_quality = MagicMock()
        service._observe_answer_quality = MagicMock()

        mock_reranker = MagicMock()
        mock_reranker.rerank.side_effect = RuntimeError("fail")
        service._reranker = mock_reranker

        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        with patch("rag_system.service.get_circuit_breaker", return_value=mock_cb):
            with patch.object(metrics, "increment") as mock_increment:
                service.query(QueryRequest(question="test?"))

        error_calls = [
            c
            for c in mock_increment.call_args_list
            if c[0][0] == "rag_reranker_fallback_total" and c[0][1].get("reason") == "error"
        ]
        assert len(error_calls) == 1
