def test_root_returns_ok(fastapi_client) -> None:
    response = fastapi_client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_returns_ok(fastapi_client) -> None:
    response = fastapi_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_returns_prometheus_format(fastapi_client) -> None:
    response = fastapi_client.get("/metrics")
    assert response.status_code == 200
    assert "rag_build_info" in response.text


def test_query_invalid_body_returns_422(fastapi_client) -> None:
    response = fastapi_client.post("/query", json={"question": ""})
    assert response.status_code == 422


def test_copilot_query_invalid_body_returns_422(fastapi_client) -> None:
    response = fastapi_client.post("/copilot/query", json={"question": ""})
    assert response.status_code == 422


def test_ask_invalid_body_returns_422(fastapi_client) -> None:
    response = fastapi_client.post("/ask", json={"question": ""})
    assert response.status_code == 422
