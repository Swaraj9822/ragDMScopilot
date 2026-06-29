from rag_system.router import (
    QueryRoute,
    _answers_overlap,
    _compose_hybrid_sections,
    _parse_routing_response,
)


def test_parse_valid_rag_route() -> None:
    raw = '{"route": "rag", "reasoning": "asks about policy docs", "confidence": 0.95}'
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.rag
    assert decision.confidence == 0.95
    assert "policy" in decision.reasoning


def test_parse_valid_database_route() -> None:
    raw = '{"route": "database", "reasoning": "asks for metrics", "confidence": 0.9}'
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.database


def test_parse_valid_hybrid_route() -> None:
    raw = '{"route": "hybrid", "reasoning": "needs both", "confidence": 0.85}'
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.hybrid


def test_parse_fenced_json() -> None:
    raw = '```json\n{"route": "database", "reasoning": "data query", "confidence": 0.9}\n```'
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.database


def test_parse_invalid_json_falls_back_to_rag() -> None:
    raw = "I think this should go to the database"
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.rag
    assert decision.confidence == 0.5


def test_parse_unknown_route_falls_back_to_rag() -> None:
    raw = '{"route": "unknown_thing", "reasoning": "test", "confidence": 0.8}'
    decision = _parse_routing_response(raw)
    assert decision.route == QueryRoute.rag


# ---------------------------------------------------------------------------
# Hybrid overlap detection
# ---------------------------------------------------------------------------


def test_overlapping_answers_are_detected() -> None:
    rag = "Our refund policy allows returns within thirty days of purchase."
    db = "Refund transactions totalled 412 returns within the purchase window this quarter."
    # Shared significant tokens: refund, returns, within, purchase
    assert _answers_overlap(rag, db, threshold=0.12) is True


def test_disjoint_answers_do_not_overlap() -> None:
    rag = "The employee handbook describes vacation accrual and parental leave."
    db = "Server uptime averaged 99.97 percent across the monitored regions."
    assert _answers_overlap(rag, db, threshold=0.12) is False


def test_empty_answer_never_overlaps() -> None:
    assert _answers_overlap("", "anything meaningful here", threshold=0.0) is False
    assert _answers_overlap("anything meaningful here", "", threshold=0.0) is False


def test_higher_threshold_requires_more_overlap() -> None:
    rag = "Quarterly revenue grew alongside customer retention and renewal rates."
    db = "Revenue figures show quarterly growth in renewal numbers."
    assert _answers_overlap(rag, db, threshold=0.1) is True
    assert _answers_overlap(rag, db, threshold=0.95) is False


# ---------------------------------------------------------------------------
# Hybrid section composition
# ---------------------------------------------------------------------------


def test_compose_sections_includes_both_labeled_sections() -> None:
    composed = _compose_hybrid_sections("Document answer.", "Data answer.")
    assert "## From documents" in composed
    assert "## From data" in composed
    assert "Document answer." in composed
    assert "Data answer." in composed
    # Document section precedes the data section.
    assert composed.index("## From documents") < composed.index("## From data")


def test_compose_sections_skips_empty_source() -> None:
    composed = _compose_hybrid_sections("Only documents here.", "   ")
    assert "## From documents" in composed
    assert "## From data" not in composed
