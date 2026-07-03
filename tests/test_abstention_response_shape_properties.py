"""Property-based test for the abstention response shape (task 3.3).

Feature: rag-trust-and-observability, Property 10: Abstention responses carry
no answer content and a bounded description

This drives :func:`rag_system.abstention.evaluate_abstention` across a broad,
intelligently-constrained space of answer-path states that exercise every one
of the six triggers, and asserts the response-shape invariant of R3.7 / R3.8:
for *any* produced ``AbstentionResponse`` the response carries no answer,
claims, or evidence content, includes exactly one ``reason_code`` drawn from the
defined set, and includes a ``missing_information`` description whose length is
between 1 and 1000 characters inclusive.

Validates: Requirements 3.7, 3.8
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.abstention import evaluate_abstention
from rag_system.models import (
    AbstentionResponse,
    AnswerSpan,
    Claim,
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    ReasonCode,
    VerificationResult,
)

# The exact set of fields an AbstentionResponse may carry — no answer, claims,
# or evidence content is permitted (R3.7).
_ALLOWED_FIELDS = {"reason_code", "missing_information", "trace_id"}

_verification = st.sampled_from(list(VerificationResult))
_coverage = st.sampled_from(list(EvidenceCoverage))
_status = st.sampled_from(list(EvidenceStatus))
_routes = st.sampled_from(["rag", "database", "hybrid"])


@st.composite
def _document_items(draw: st.DrawFn) -> EvidenceItem:
    start = draw(st.integers(min_value=0, max_value=50))
    end = draw(st.integers(min_value=start, max_value=start + 50))
    return EvidenceItem(
        kind="document",
        verification_result=draw(_verification),
        coverage=draw(_coverage),
        quote=draw(st.text(min_size=1, max_size=20)),
        source_start=start,
        source_end=end,
        # A small pool of document ids so conflicting-evidence cases (same and
        # different sources) both arise naturally.
        document_id=draw(st.sampled_from(["doc-a", "doc-b", "doc-c"])),
        document_version="v1",
    )


@st.composite
def _claims(draw: st.DrawFn) -> Claim:
    items = draw(st.lists(_document_items(), max_size=4))
    return Claim(
        claim_id=draw(st.text(min_size=1, max_size=8)),
        text=draw(st.text(min_size=1, max_size=20)),
        answer_span=AnswerSpan(start=0, end=1),
        evidence_items=items,
        evidence_status=draw(_status),
    )


@st.composite
def _missing_information_overrides(draw: st.DrawFn) -> dict[ReasonCode, str] | None:
    """Optionally override descriptions, including empty / whitespace / oversized
    values, to stress the 1..1000 clamp/fallback in R3.8."""
    if draw(st.booleans()):
        return None
    override_text = st.one_of(
        st.just(""),
        st.just("   "),
        st.text(min_size=1, max_size=50),
        st.just("x" * 5000),  # exceeds the 1000-char bound; must be clamped
    )
    return draw(
        st.dictionaries(
            keys=st.sampled_from(list(ReasonCode)),
            values=override_text,
            max_size=len(ReasonCode),
        )
    )


@st.composite
def _abstention_kwargs(draw: st.DrawFn) -> dict:
    return dict(
        trace_id=draw(st.text(min_size=1, max_size=16)),
        route=draw(_routes),
        retrieval_scores=draw(
            st.one_of(
                st.none(),
                st.just([]),
                st.lists(
                    st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=6
                ),
            )
        ),
        retrieval_score_threshold=draw(st.floats(min_value=0.0, max_value=1.0)),
        confidence_score=draw(
            st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
        ),
        route_min_confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
        claims=draw(st.lists(_claims(), max_size=5)),
        conflicting_claim_ids=draw(
            st.one_of(
                st.none(),
                st.sets(st.text(min_size=1, max_size=8), max_size=4),
            )
        ),
        sql_row_count=draw(
            st.one_of(st.none(), st.integers(min_value=0, max_value=100))
        ),
        missing_information=draw(_missing_information_overrides()),
    )


# Feature: rag-trust-and-observability, Property 10: Abstention responses carry no answer content and a bounded description
@settings(max_examples=400)
@given(kwargs=_abstention_kwargs())
def test_abstention_response_shape(kwargs: dict) -> None:
    result = evaluate_abstention(**kwargs)

    # The property is universally quantified over abstention *responses*; when no
    # trigger fires there is nothing to check.
    if result is None:
        return

    assert isinstance(result, AbstentionResponse)

    # Exactly one reason code, drawn from the defined set (R3.8).
    assert isinstance(result.reason_code, ReasonCode)
    assert result.reason_code in set(ReasonCode)

    # Bounded 1..1000 char missing-information description (R3.8).
    assert 1 <= len(result.missing_information) <= 1000

    # No answer / claims / evidence content — only the three permitted fields
    # exist on the response (R3.7).
    dumped = result.model_dump()
    assert set(dumped) == _ALLOWED_FIELDS
    for forbidden in ("answer", "claims", "evidence_items", "evidence", "citations"):
        assert forbidden not in dumped
