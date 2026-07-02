"""Regression tests for HTTP metric label cardinality (finding 4).

The request middleware labels ``rag_http_requests_total`` /
``rag_http_request_duration_ms`` with the matched *route template*
(``/documents/{document_id}``) rather than the concrete URL path
(``/documents/<uuid>``). Labelling with the raw path minted a new Prometheus
series per unique id, growing the in-process registry without bound (a slow
memory leak) and swamping the exposition with one-off series.

These tests drive several distinct concrete paths through the middleware and
assert that only the bounded, templated label appears — never the per-id paths.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_system import api as api_module


class _MissingDocService:
    """Stand-in service whose document lookups always 404 (no AWS needed)."""

    def get_document(self, document_id: str):
        return None


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(api_module, "get_service", lambda: _MissingDocService())
    return TestClient(api_module.app)


def test_per_id_document_paths_collapse_to_a_single_templated_series(monkeypatch) -> None:
    client = _client(monkeypatch)

    # Three distinct, unmistakable document ids through the same route.
    ids = ["cardinality-alpha", "cardinality-bravo", "cardinality-charlie"]
    for document_id in ids:
        assert client.get(f"/documents/{document_id}").status_code == 404

    body = client.get("/metrics").text

    # The bounded template label is present...
    assert 'path="/documents/{document_id}"' in body
    # ...and none of the concrete ids leaked into a label (unbounded series).
    for document_id in ids:
        assert f"/documents/{document_id}" not in body


def test_unmatched_paths_share_a_single_sentinel_series(monkeypatch) -> None:
    client = _client(monkeypatch)

    # Paths that match no route must not each create their own label set.
    for suffix in ("no-such-route-1", "no-such-route-2", "no-such-route-3"):
        assert client.get(f"/does/not/exist/{suffix}").status_code == 404

    body = client.get("/metrics").text

    assert 'path="__unmatched__"' in body
    for suffix in ("no-such-route-1", "no-such-route-2", "no-such-route-3"):
        assert suffix not in body


def test_static_routes_still_use_their_own_path_label(monkeypatch) -> None:
    client = _client(monkeypatch)

    assert client.get("/health").status_code == 200

    body = client.get("/metrics").text

    # A param-free route keeps its literal path (template == path here).
    assert 'path="/health"' in body
