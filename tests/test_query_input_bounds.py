"""Input bounds on user-supplied query fields (finding #9).

An unbounded ``question`` flows straight into paid, latency-bound LLM prompts,
and an unbounded ``document_ids`` list lets a request body grow without limit.
Both are now capped; over-limit values are rejected by pydantic validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag_system.models import (
    MAX_DOCUMENT_IDS,
    MAX_QUESTION_CHARS,
    CopilotQueryRequest,
    QueryRequest,
    ReplayRunRequest,
    ReplayRetrievalParams,
    UnifiedQueryRequest,
)

_QUESTION_MODELS = [QueryRequest, UnifiedQueryRequest, CopilotQueryRequest]


@pytest.mark.parametrize("model", _QUESTION_MODELS)
def test_question_at_limit_is_accepted(model) -> None:
    model(question="x" * MAX_QUESTION_CHARS)  # no raise


@pytest.mark.parametrize("model", _QUESTION_MODELS)
def test_question_over_limit_is_rejected(model) -> None:
    with pytest.raises(ValidationError):
        model(question="x" * (MAX_QUESTION_CHARS + 1))


@pytest.mark.parametrize("model", [QueryRequest, UnifiedQueryRequest])
def test_document_ids_over_limit_is_rejected(model) -> None:
    with pytest.raises(ValidationError):
        model(question="hi", document_ids=[str(i) for i in range(MAX_DOCUMENT_IDS + 1)])


def test_document_ids_at_limit_is_accepted() -> None:
    QueryRequest(question="hi", document_ids=[str(i) for i in range(MAX_DOCUMENT_IDS)])


def test_replay_run_request_question_bounded() -> None:
    params = ReplayRetrievalParams(max_passages=10, min_score=0.5)
    with pytest.raises(ValidationError):
        ReplayRunRequest(
            question="x" * (MAX_QUESTION_CHARS + 1),
            ai_configuration_version_id="cfg",
            retrieval_params=params,
        )
