"""/metrics endpoint compatibility snapshot test (task 16.5).

Validates that the ``/metrics`` endpoint continues to expose every metric name
and label set that existed *before* the tracing layer was added. The tracing
platform adds new counters (``rag_traces_persisted_total``,
``rag_spans_dropped_total``, etc.) to the shared :class:`MetricsRegistry` but
must never remove, rename, or shadow the pre-existing metrics.

**Validates: Requirements 11.2**

Strategy:
1. Exercise the ``/metrics`` endpoint via the FastAPI TestClient.
2. Assert the response is 200 with the correct ``text/plain`` content-type.
3. Assert the format is valid Prometheus text exposition (TYPE lines, metric
   lines, comments).
4. Emit a representative sample of each pre-existing metric and verify they
   all appear in the endpoint output (since the shared registry only renders
   metrics once they have been emitted at least once).
5. Assert that none of the new tracing metrics shadow or replace the old ones
   by confirming the old metric names still appear with correct TYPE metadata.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.observability import metrics


# ---------------------------------------------------------------------------
# Pre-existing metric names and their expected Prometheus types.
#
# These are the metrics emitted by the ``timed()`` helper, the ``log_requests``
# middleware, and various ``service.py`` / ``api.py`` call sites that existed
# BEFORE the observability-tracing layer was introduced.
# ---------------------------------------------------------------------------

#: (metric_name, expected_prometheus_type)
PRE_EXISTING_METRICS: list[tuple[str, str]] = [
    ("rag_operation_total", "counter"),
    ("rag_operation_duration_ms", "summary"),
    ("rag_http_requests_total", "counter"),
    ("rag_http_request_duration_ms", "summary"),
    ("rag_queries_total", "counter"),
    ("rag_query_length_chars", "summary"),
    ("rag_documents_queued_total", "counter"),
    ("rag_documents_deleted_total", "counter"),
    ("rag_query_traces_stored_total", "counter"),
    ("rag_query_trace_latency_ms", "summary"),
    ("rag_query_feedback_total", "counter"),
    ("rag_evidence_status_total", "counter"),
    ("rag_answer_citation_count", "summary"),
    ("rag_answer_context_hit_count", "summary"),
    ("rag_answer_without_citations_total", "counter"),
    ("rag_retrieval_zero_hit_total", "counter"),
    ("rag_retrieval_low_top_score_total", "counter"),
    ("rag_retrieval_dominant_doc_ratio", "summary"),
]

#: New tracing-layer metrics that MUST NOT replace/shadow any pre-existing ones.
NEW_TRACING_METRICS: list[str] = [
    "rag_traces_persisted_total",
    "rag_trace_store_write_failures_total",
    "rag_spans_dropped_total",
    "rag_logs_dropped_total",
    "rag_trace_context_propagation_failures_total",
    "rag_log_store_write_failures_total",
    "rag_trace_store_retention_failures_total",
    "rag_log_store_retention_failures_total",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_pre_existing_metrics() -> None:
    """Emit at least one sample of every pre-existing metric so they appear in
    the /metrics output (the MetricsRegistry only renders metrics that have been
    emitted at least once).
    """
    # Counters
    metrics.increment("rag_operation_total", {"operation": "test_op", "status": "success"})
    metrics.increment("rag_http_requests_total", {"method": "GET", "path": "/health", "status_code": "200"})
    metrics.increment("rag_queries_total", {"mode": "dense"})
    metrics.increment("rag_documents_queued_total")
    metrics.increment("rag_documents_deleted_total")
    metrics.increment("rag_query_traces_stored_total", {"route": "rag"})
    metrics.increment("rag_query_feedback_total", {"rating": "positive"})
    metrics.increment("rag_evidence_status_total", {"status": "grounded"})
    metrics.increment("rag_answer_without_citations_total", {"mode": "dense"})
    metrics.increment("rag_retrieval_zero_hit_total", {"mode": "dense"})
    metrics.increment("rag_retrieval_low_top_score_total", {"mode": "dense"})

    # Summaries (observe)
    metrics.observe("rag_operation_duration_ms", 42.0, {"operation": "test_op", "status": "success"})
    metrics.observe("rag_http_request_duration_ms", 15.0, {"method": "GET", "path": "/health", "status_code": "200"})
    metrics.observe("rag_query_length_chars", 100.0, {"mode": "dense"})
    metrics.observe("rag_query_trace_latency_ms", 200.0, {"route": "rag"})
    metrics.observe("rag_answer_citation_count", 3.0, {"mode": "dense", "evidence_status": "grounded"})
    metrics.observe("rag_answer_context_hit_count", 5.0, {"mode": "dense", "evidence_status": "grounded"})
    metrics.observe("rag_retrieval_dominant_doc_ratio", 0.6, {"mode": "dense"})


def _get_client() -> TestClient:
    """Build a TestClient against the actual app."""
    return TestClient(api_module.app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMetricsEndpointCompatibility:
    """Assert /metrics endpoint preserves all pre-existing metric output."""

    def test_metrics_endpoint_returns_200_with_prometheus_content_type(self) -> None:
        """The /metrics endpoint responds with HTTP 200 and the correct
        Prometheus text exposition content type (text/plain with version).
        """
        client = _get_client()
        response = client.get("/metrics")

        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "text/plain" in content_type

    def test_metrics_output_is_valid_prometheus_text_format(self) -> None:
        """The /metrics output contains only valid Prometheus text exposition
        lines: comments (# ...), TYPE declarations, HELP declarations, and
        metric lines (name{labels} value).
        """
        _emit_pre_existing_metrics()
        client = _get_client()
        response = client.get("/metrics")

        body = response.text
        assert body.strip(), "Metrics output must not be empty"

        # Every non-empty line must be a comment or a metric sample line.
        metric_line_re = re.compile(
            r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})?\s+[\d.eE+\-]+$"
        )
        comment_re = re.compile(r"^#\s")

        for line in body.strip().splitlines():
            if not line.strip():
                continue
            assert comment_re.match(line) or metric_line_re.match(line), (
                f"Invalid Prometheus text line: {line!r}"
            )

    def test_all_pre_existing_metric_names_present(self) -> None:
        """Every pre-existing metric name appears in the /metrics output after
        those metrics have been emitted at least once.
        """
        _emit_pre_existing_metrics()
        client = _get_client()
        response = client.get("/metrics")
        body = response.text

        for metric_name, _ in PRE_EXISTING_METRICS:
            assert metric_name in body, (
                f"Pre-existing metric '{metric_name}' not found in /metrics output"
            )

    def test_pre_existing_metric_types_correct(self) -> None:
        """Each pre-existing metric has the correct TYPE declaration (counter
        or summary) in the output, confirming the registry type has not changed.
        """
        _emit_pre_existing_metrics()
        client = _get_client()
        response = client.get("/metrics")
        body = response.text

        for metric_name, expected_type in PRE_EXISTING_METRICS:
            type_line = f"# TYPE {metric_name} {expected_type}"
            assert type_line in body, (
                f"Expected TYPE declaration '{type_line}' not found in /metrics output"
            )

    def test_new_tracing_metrics_do_not_shadow_pre_existing(self) -> None:
        """New tracing-layer metrics have distinct names that do not collide
        with any of the pre-existing metric names.
        """
        pre_existing_names = {name for name, _ in PRE_EXISTING_METRICS}
        for new_metric in NEW_TRACING_METRICS:
            assert new_metric not in pre_existing_names, (
                f"Tracing metric '{new_metric}' shadows a pre-existing metric"
            )

    def test_pre_existing_counter_labels_preserved(self) -> None:
        """Spot-check that pre-existing counter metrics retain their original
        label sets (operation/status for rag_operation_total, method/path/status_code
        for rag_http_requests_total).
        """
        _emit_pre_existing_metrics()
        client = _get_client()
        response = client.get("/metrics")
        body = response.text

        # rag_operation_total with operation and status labels
        assert 'rag_operation_total{operation="test_op",status="success"}' in body

        # rag_http_requests_total with method, path, and status_code labels
        assert 'rag_http_requests_total{' in body
        assert 'method="GET"' in body
        assert 'status_code="200"' in body

    def test_pre_existing_summary_quantiles_present(self) -> None:
        """Spot-check that pre-existing summary metrics include quantile lines
        (0.5, 0.95, 0.99), _count, and _sum suffixes.
        """
        _emit_pre_existing_metrics()
        client = _get_client()
        response = client.get("/metrics")
        body = response.text

        # rag_operation_duration_ms should have quantile, count, sum lines
        assert 'rag_operation_duration_ms{' in body
        assert 'quantile="0.5"' in body
        assert "rag_operation_duration_ms_count{" in body
        assert "rag_operation_duration_ms_sum{" in body

    def test_build_info_gauge_still_present(self) -> None:
        """The static rag_build_info gauge (always rendered first) is still
        present and unaltered.
        """
        client = _get_client()
        response = client.get("/metrics")
        body = response.text

        assert 'rag_build_info{service="production-rag"} 1' in body
