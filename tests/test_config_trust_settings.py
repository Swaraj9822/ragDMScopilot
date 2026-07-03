"""Tests for the RAG trust & observability configuration settings.

Feature: rag-trust-and-observability (task 1.3).

These tests exercise the settings and thresholds added to ``Settings`` in
``src/rag_system/config.py``: answer-path/abstention thresholds, corpus/listing
knobs, evaluation/judge settings, trace-investigator settings, knowledge-gap
settings, and replay settings (including ``model_pricing``).

The ``Settings`` model uses field *aliases* (e.g. ``RAG_CORPUS_PAGE_SIZE``), so
values are supplied by alias exactly as they would arrive from the environment.
Other required settings are supplied with throwaway values so the model can be
constructed in isolation without depending on a ``.env`` file.
"""

import pydantic
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.config import ModelPricing, Settings

_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(**overrides: object) -> Settings:
    """Construct ``Settings`` with the required aliases plus any overrides."""
    return Settings(**_REQUIRED_BY_ALIAS, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_match_spec() -> None:
    """All new settings default to the values specified by task 1.3."""
    config = _build_settings()

    # answer-path / abstention
    assert config.route_min_confidence == 0.5
    assert config.retrieval_score_threshold == 0.3
    assert config.clarification_expiry_minutes == 30

    # corpus / listing
    assert config.corpus_page_size == 50
    assert config.pagination_signing_key is None

    # evaluation / judge
    assert config.retrieval_metric_depth_k == 10
    assert config.llm_judge_model_id == "gemini-3.1-pro"
    assert config.llm_judge_thinking_budget == 4096
    assert config.llm_judge_read_timeout_s == 55
    assert config.llm_judge_per_case_timeout_s == 60
    assert config.llm_judge_schedule_interval_hours == 24

    # trace investigator
    assert config.trace_investigator_model_id == "gemini-3.1-pro"
    assert config.trace_investigator_thinking_budget == 4096
    assert config.trace_investigator_read_timeout_s == 55

    # knowledge gap
    assert config.knowledge_gap_max_topics == 25
    assert config.knowledge_gap_min_eligible_outcomes == 20

    # replay
    assert config.replay_job_timeout_s == 300


def test_default_model_pricing_covers_both_models() -> None:
    """Default pricing map carries entries for both known model ids."""
    config = _build_settings()

    assert set(config.model_pricing) >= {"gemini-3.5-flash", "gemini-3.1-pro"}
    for entry in config.model_pricing.values():
        assert isinstance(entry, ModelPricing)
        assert entry.prompt_usd_per_1k >= 0.0
        assert entry.completion_usd_per_1k >= 0.0


def test_llm_judge_read_timeout_fits_inside_per_case_timeout() -> None:
    """The judge read timeout is sized to fit inside the per-case timeout (R7.8)."""
    config = _build_settings()
    assert config.llm_judge_read_timeout_s < config.llm_judge_per_case_timeout_s


# ---------------------------------------------------------------------------
# Bounds - corpus page size (must be within [1, 100])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 25, 50, 100])
def test_corpus_page_size_accepts_in_range(value: int) -> None:
    assert _build_settings(RAG_CORPUS_PAGE_SIZE=value).corpus_page_size == value


@pytest.mark.parametrize("value", [0, -1, 101, 1000])
def test_corpus_page_size_rejects_out_of_range(value: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        _build_settings(RAG_CORPUS_PAGE_SIZE=value)


# ---------------------------------------------------------------------------
# Bounds - confidence / score thresholds (must be within [0.0, 1.0])
# ---------------------------------------------------------------------------


@given(
    value=st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
    )
)
@settings(max_examples=50)
def test_route_min_confidence_accepts_unit_interval(value: float) -> None:
    assert _build_settings(RAG_ROUTE_MIN_CONFIDENCE=value).route_min_confidence == value


@given(
    value=st.floats(allow_nan=False, allow_infinity=False).filter(
        lambda x: x < 0.0 or x > 1.0
    )
)
@settings(max_examples=50)
def test_retrieval_score_threshold_rejects_out_of_unit_interval(value: float) -> None:
    with pytest.raises(pydantic.ValidationError):
        _build_settings(RAG_RETRIEVAL_SCORE_THRESHOLD=value)


# ---------------------------------------------------------------------------
# Bounds - thinking budgets (must be within [0, 32768])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 4096, 32768])
def test_thinking_budget_accepts_in_range(value: int) -> None:
    config = _build_settings(
        RAG_LLM_JUDGE_THINKING_BUDGET=value,
        RAG_TRACE_INVESTIGATOR_THINKING_BUDGET=value,
    )
    assert config.llm_judge_thinking_budget == value
    assert config.trace_investigator_thinking_budget == value


@pytest.mark.parametrize("value", [-1, 32769, 1_000_000])
def test_thinking_budget_rejects_out_of_range(value: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        _build_settings(RAG_LLM_JUDGE_THINKING_BUDGET=value)
    with pytest.raises(pydantic.ValidationError):
        _build_settings(RAG_TRACE_INVESTIGATOR_THINKING_BUDGET=value)


# ---------------------------------------------------------------------------
# Bounds - positive time windows / counts (must be within [1, 100000])
# ---------------------------------------------------------------------------

_POSITIVE_BOUNDED_ALIASES = [
    "RAG_CLARIFICATION_EXPIRY_MINUTES",
    "RAG_RETRIEVAL_METRIC_DEPTH_K",
    "RAG_LLM_JUDGE_READ_TIMEOUT_S",
    "RAG_LLM_JUDGE_PER_CASE_TIMEOUT_S",
    "RAG_LLM_JUDGE_SCHEDULE_INTERVAL_HOURS",
    "RAG_TRACE_INVESTIGATOR_READ_TIMEOUT_S",
    "RAG_KNOWLEDGE_GAP_MAX_TOPICS",
    "RAG_KNOWLEDGE_GAP_MIN_ELIGIBLE_OUTCOMES",
    "RAG_REPLAY_JOB_TIMEOUT_S",
]


@pytest.mark.parametrize("alias", _POSITIVE_BOUNDED_ALIASES)
@pytest.mark.parametrize("value", [0, -1, 100_001])
def test_positive_bounded_settings_reject_out_of_range(alias: str, value: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        _build_settings(**{alias: value})


@pytest.mark.parametrize("alias", _POSITIVE_BOUNDED_ALIASES)
def test_positive_bounded_settings_accept_in_range(alias: str) -> None:
    # Constructs cleanly with a valid in-range value.
    _build_settings(**{alias: 5})


# ---------------------------------------------------------------------------
# model_pricing overrides and validation
# ---------------------------------------------------------------------------


def test_model_pricing_override_from_mapping() -> None:
    config = _build_settings(
        RAG_MODEL_PRICING={
            "custom-model": {"prompt_usd_per_1k": 0.01, "completion_usd_per_1k": 0.02}
        }
    )
    assert config.model_pricing["custom-model"].prompt_usd_per_1k == 0.01
    assert config.model_pricing["custom-model"].completion_usd_per_1k == 0.02


def test_model_pricing_rejects_negative_price() -> None:
    with pytest.raises(pydantic.ValidationError):
        _build_settings(
            RAG_MODEL_PRICING={
                "bad": {"prompt_usd_per_1k": -1.0, "completion_usd_per_1k": 0.0}
            }
        )
