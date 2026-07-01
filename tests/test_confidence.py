"""Tests for the deterministic confidence scoring in rag_system.confidence."""

from __future__ import annotations

import math

import pytest

from rag_system.confidence import (
    MAX_SCORE,
    MIN_SCORE,
    combine_confidence_scores,
    confidence_band,
    database_confidence_score,
    rag_confidence_score,
)


# --- rag_confidence_score --------------------------------------------------


def test_grounded_evidence_drives_a_high_score():
    score = rag_confidence_score(evidence_status="grounded")
    assert score == pytest.approx(0.85, abs=1e-3)


def test_insufficient_evidence_is_low():
    assert rag_confidence_score(evidence_status="insufficient_evidence") < 0.4


def test_unknown_evidence_status_uses_default_base():
    assert rag_confidence_score(evidence_status="something-weird") == pytest.approx(0.4, abs=1e-3)


def test_has_insufficient_reason_caps_score():
    capped = rag_confidence_score(evidence_status="grounded", has_insufficient_reason=True)
    assert capped <= 0.35


def test_retrieval_and_citation_grounding_contributes():
    """Strong retrieval scores and full citation coverage should raise the score
    relative to the same answer with weak, uncited retrieval."""
    strong = rag_confidence_score(
        evidence_status="partially_grounded",
        retrieval_scores=[0.9, 0.85],
        citation_count=2,
        hit_count=2,
    )
    weak = rag_confidence_score(
        evidence_status="partially_grounded",
        retrieval_scores=[0.1, 0.05],
        citation_count=0,
        hit_count=2,
    )
    assert strong > weak


def test_logprob_signal_is_folded_in():
    high_lp = rag_confidence_score(evidence_status="grounded", avg_logprob=math.log(0.95))
    low_lp = rag_confidence_score(evidence_status="grounded", avg_logprob=math.log(0.2))
    assert high_lp > low_lp


def test_model_confidence_label_matters():
    high = rag_confidence_score(evidence_status="grounded", model_confidence="high")
    low = rag_confidence_score(evidence_status="grounded", model_confidence="low")
    assert high > low


@pytest.mark.parametrize(
    "kwargs",
    [
        {"evidence_status": "grounded", "model_confidence": "high",
         "retrieval_scores": [1.0, 1.0], "citation_count": 5, "hit_count": 5,
         "avg_logprob": 0.0},
        {"evidence_status": "insufficient_evidence", "has_insufficient_reason": True},
        {"evidence_status": "no_rows", "retrieval_scores": [], "hit_count": 0},
    ],
)
def test_rag_score_always_within_bounds(kwargs):
    score = rag_confidence_score(**kwargs)
    assert MIN_SCORE <= score <= MAX_SCORE


# --- database_confidence_score ---------------------------------------------


def test_unvalidated_sql_collapses_to_floor():
    assert database_confidence_score(evidence_status="grounded", sql_validated=False) == MIN_SCORE


def test_grounded_with_rows_beats_grounded_without_rows():
    with_rows = database_confidence_score(evidence_status="grounded", row_count=5)
    no_rows = database_confidence_score(evidence_status="grounded", row_count=0)
    assert with_rows > no_rows


def test_database_score_within_bounds():
    score = database_confidence_score(evidence_status="grounded", row_count=10, avg_logprob=0.0)
    assert MIN_SCORE <= score <= MAX_SCORE


# --- combine_confidence_scores ---------------------------------------------


def test_combine_returns_none_when_no_scores():
    assert combine_confidence_scores([None, None]) is None
    assert combine_confidence_scores([]) is None


def test_combine_is_conservative_biased_toward_minimum():
    """Blending mean and min means the result sits below the plain average."""
    scores = [0.9, 0.3]
    plain_mean = sum(scores) / len(scores)
    combined = combine_confidence_scores(scores)
    assert combined is not None
    assert combined < plain_mean
    assert MIN_SCORE <= combined <= MAX_SCORE


def test_combine_ignores_none_entries():
    assert combine_confidence_scores([0.8, None]) == pytest.approx(0.8, abs=1e-3)


# --- confidence_band -------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [(None, None), (0.95, "high"), (0.7, "high"), (0.69, "medium"),
     (0.4, "medium"), (0.39, "low"), (0.0, "low")],
)
def test_confidence_band(score, expected):
    assert confidence_band(score) == expected
