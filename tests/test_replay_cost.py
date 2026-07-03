"""Tests for replay cost computation (R8.7, task 14.14).

Covers the ``compute_replay_cost`` pure function:
- Known model computes cost from the formula
- Unknown model returns 0.0 and logs a warning
- Zero tokens yield zero cost
- Edge cases: single token type, large token counts
"""

from __future__ import annotations

import logging

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.config import ModelPricing
from rag_system.replay import compute_replay_cost


# ---------------------------------------------------------------------------
# Fixtures / shared data
# ---------------------------------------------------------------------------

_PRICING_MAP: dict[str, ModelPricing] = {
    "gemini-3.5-flash": ModelPricing(
        prompt_usd_per_1k=0.000075, completion_usd_per_1k=0.0003
    ),
    "gemini-3.1-pro": ModelPricing(
        prompt_usd_per_1k=0.00125, completion_usd_per_1k=0.005
    ),
}


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_known_model_computes_cost_correctly() -> None:
    """cost = prompt_tokens / 1000 * price_in + completion_tokens / 1000 * price_out"""
    cost = compute_replay_cost(
        prompt_tokens=2000,
        completion_tokens=1000,
        model_id="gemini-3.5-flash",
        pricing_map=_PRICING_MAP,
    )
    # 2000/1000 * 0.000075 + 1000/1000 * 0.0003 = 0.00015 + 0.0003 = 0.00045
    assert cost == pytest.approx(0.00045)


def test_known_model_pro_computes_cost_correctly() -> None:
    cost = compute_replay_cost(
        prompt_tokens=5000,
        completion_tokens=2000,
        model_id="gemini-3.1-pro",
        pricing_map=_PRICING_MAP,
    )
    # 5000/1000 * 0.00125 + 2000/1000 * 0.005 = 0.00625 + 0.01 = 0.01625
    assert cost == pytest.approx(0.01625)


def test_unknown_model_returns_zero(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rag_system.replay"):
        cost = compute_replay_cost(
            prompt_tokens=1000,
            completion_tokens=500,
            model_id="unknown-model-xyz",
            pricing_map=_PRICING_MAP,
        )
    assert cost == 0.0
    assert "unknown-model-xyz" in caplog.text
    assert "not found in pricing map" in caplog.text


def test_zero_tokens_yield_zero_cost() -> None:
    cost = compute_replay_cost(
        prompt_tokens=0,
        completion_tokens=0,
        model_id="gemini-3.5-flash",
        pricing_map=_PRICING_MAP,
    )
    assert cost == 0.0


def test_only_prompt_tokens() -> None:
    cost = compute_replay_cost(
        prompt_tokens=1000,
        completion_tokens=0,
        model_id="gemini-3.5-flash",
        pricing_map=_PRICING_MAP,
    )
    # 1000/1000 * 0.000075 = 0.000075
    assert cost == pytest.approx(0.000075)


def test_only_completion_tokens() -> None:
    cost = compute_replay_cost(
        prompt_tokens=0,
        completion_tokens=1000,
        model_id="gemini-3.5-flash",
        pricing_map=_PRICING_MAP,
    )
    # 1000/1000 * 0.0003 = 0.0003
    assert cost == pytest.approx(0.0003)


def test_empty_pricing_map_returns_zero(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rag_system.replay"):
        cost = compute_replay_cost(
            prompt_tokens=1000,
            completion_tokens=500,
            model_id="gemini-3.5-flash",
            pricing_map={},
        )
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@given(
    prompt_tokens=st.integers(min_value=0, max_value=1_000_000),
    completion_tokens=st.integers(min_value=0, max_value=1_000_000),
    model_id=st.sampled_from(["gemini-3.5-flash", "gemini-3.1-pro"]),
)
@settings(max_examples=200)
def test_cost_is_non_negative_for_known_models(
    prompt_tokens: int, completion_tokens: int, model_id: str
) -> None:
    """**Validates: Requirements 8.7**

    Cost must always be >= 0 for any non-negative token counts and known model.
    """
    cost = compute_replay_cost(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model_id=model_id,
        pricing_map=_PRICING_MAP,
    )
    assert cost >= 0.0


@given(
    prompt_tokens=st.integers(min_value=0, max_value=1_000_000),
    completion_tokens=st.integers(min_value=0, max_value=1_000_000),
    model_id=st.text(min_size=1, max_size=50).filter(
        lambda m: m not in ("gemini-3.5-flash", "gemini-3.1-pro")
    ),
)
@settings(max_examples=100)
def test_unknown_model_always_returns_zero(
    prompt_tokens: int, completion_tokens: int, model_id: str
) -> None:
    """**Validates: Requirements 8.7**

    A model absent from the pricing map always yields 0.0 cost.
    """
    cost = compute_replay_cost(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model_id=model_id,
        pricing_map=_PRICING_MAP,
    )
    assert cost == 0.0


@given(
    prompt_tokens=st.integers(min_value=0, max_value=1_000_000),
    completion_tokens=st.integers(min_value=0, max_value=1_000_000),
    prompt_price=st.floats(min_value=0.0, max_value=1.0),
    completion_price=st.floats(min_value=0.0, max_value=1.0),
)
@settings(max_examples=200)
def test_cost_matches_formula(
    prompt_tokens: int,
    completion_tokens: int,
    prompt_price: float,
    completion_price: float,
) -> None:
    """**Validates: Requirements 8.7**

    The computed cost must equal the formula:
    cost = prompt_tokens / 1000 * price_in + completion_tokens / 1000 * price_out
    """
    pricing_map = {
        "test-model": ModelPricing(
            prompt_usd_per_1k=prompt_price,
            completion_usd_per_1k=completion_price,
        )
    }
    cost = compute_replay_cost(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model_id="test-model",
        pricing_map=pricing_map,
    )
    expected = (
        prompt_tokens / 1000 * prompt_price
        + completion_tokens / 1000 * completion_price
    )
    assert cost == pytest.approx(expected)
