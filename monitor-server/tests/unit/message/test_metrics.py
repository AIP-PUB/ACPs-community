"""单元测试：G-1 metrics.py — 指标注册表与 canonical 键名（设计 §9.1）。"""

from __future__ import annotations


class TestMessageMetrics:
    def test_import(self) -> None:
        from app.message.metrics import MessageMetrics

        assert MessageMetrics is not None

    def test_singleton_exists(self) -> None:
        from app.message.metrics import metrics

        assert metrics is not None

    def test_inc_increases_counter(self) -> None:
        from app.message.metrics import MessageMetrics

        m = MessageMetrics()
        m.inc("amp_message_writer_accepted_total", by=5)
        snap = m.snapshot()
        assert snap["amp_message_writer_accepted_total"] == 5

    def test_inc_default_by_one(self) -> None:
        from app.message.metrics import MessageMetrics

        m = MessageMetrics()
        m.inc("amp_message_insert_failures_total")
        m.inc("amp_message_insert_failures_total")
        assert m.snapshot()["amp_message_insert_failures_total"] == 2

    def test_observe_sets_value(self) -> None:
        from app.message.metrics import MessageMetrics

        m = MessageMetrics()
        m.observe("amp_message_insert_latency_ms", 42.5)
        assert m.snapshot()["amp_message_insert_latency_ms"] == 42.5

    def test_set_gauge(self) -> None:
        from app.message.metrics import MessageMetrics

        m = MessageMetrics()
        m.set_gauge("amp_message_read_model_lag_ms", 1234.0)
        assert m.snapshot()["amp_message_read_model_lag_ms"] == 1234.0

    def test_snapshot_is_copy(self) -> None:
        from app.message.metrics import MessageMetrics

        m = MessageMetrics()
        m.inc("amp_message_writer_accepted_total")
        snap = m.snapshot()
        snap["amp_message_writer_accepted_total"] = 999
        assert m.snapshot()["amp_message_writer_accepted_total"] == 1


CANONICAL_METRIC_NAMES = [
    # Writer / store
    "amp_message_writer_accepted_total",
    "amp_message_writer_synthetic_lifecycle_keys_total",
    "amp_message_writer_dedup_unavailable_total",
    "amp_message_insert_latency_ms",
    "amp_message_insert_failures_total",
    # Lifecycle compactor
    "amp_message_lifecycle_compact_latency_ms",
    "amp_message_lifecycle_compact_failures_total",
    # Throughput compactor
    "amp_message_destination_stats_compact_latency_ms",
    "amp_message_destination_stats_compact_failures_total",
    # Collector
    "amp_message_state_snapshot_collect_latency_ms",
    "amp_message_state_snapshot_collect_failures_total",
    # Query latency
    "amp_message_events_query_latency_ms",
    "amp_message_lifecycle_query_latency_ms",
    "amp_message_destination_state_query_latency_ms",
    "amp_message_deadletter_query_latency_ms",
    "amp_message_throughput_query_latency_ms",
    # Freshness gauge
    "amp_message_read_model_lag_ms",
]


class TestCanonicalMetricNames:
    def test_all_canonical_names_are_incrementable(self) -> None:
        from app.message.metrics import MessageMetrics

        m = MessageMetrics()
        for name in CANONICAL_METRIC_NAMES:
            m.inc(name)
        snap = m.snapshot()
        for name in CANONICAL_METRIC_NAMES:
            assert name in snap


class TestMetricsLogLoop:
    def test_import(self) -> None:
        import inspect

        from app.message.metrics import metrics_log_loop

        assert inspect.iscoroutinefunction(metrics_log_loop)
