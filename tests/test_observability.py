from rag_system.observability import MetricsRegistry


def test_metrics_registry_renders_counters_and_summaries() -> None:
    registry = MetricsRegistry()

    registry.increment("rag_test_events_total", {"kind": "unit"})
    registry.observe("rag_test_duration_ms", 10, {"kind": "unit"})
    registry.observe("rag_test_duration_ms", 20, {"kind": "unit"})

    output = registry.render_prometheus()

    assert 'rag_test_events_total{kind="unit"} 1' in output
    assert 'rag_test_duration_ms{kind="unit",quantile="0.5"}' in output
    assert 'rag_test_duration_ms_count{kind="unit"} 2' in output
