from rag_system.router import QueryRoute, _parse_routing_response


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
