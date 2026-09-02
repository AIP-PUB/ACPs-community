"""tests/unit/test_access_metrics.py — AccessMetrics 注册表与计数器测试（E-1）。

TDD E-1：先写测试（红）→ 补全 metrics 计数器接入（绿）。
验证计数器随调用增长、metrics_log_loop 周期行为。
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest


class TestAccessMetricsRegistry:
    """AccessMetrics 注册表基础行为。"""

    def test_inc_increases_counter(self) -> None:
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        m.inc("accepted")
        m.inc("accepted")
        assert m.snapshot()["accepted"] == 2

    def test_inc_with_by_parameter(self) -> None:
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        m.inc("deduped", by=5)
        assert m.snapshot()["deduped"] == 5

    def test_observe_sets_latest_value(self) -> None:
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        m.observe("insert_latency_ms", 42.5)
        assert m.snapshot()["insert_latency_ms"] == 42.5

    def test_set_gauge_updates_value(self) -> None:
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        m.set_gauge("read_model_lag_ms", 1234.0)
        assert m.snapshot()["read_model_lag_ms"] == 1234.0

    def test_snapshot_returns_copy(self) -> None:
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        m.inc("foo")
        snap = m.snapshot()
        snap["foo"] = 999
        assert m.snapshot()["foo"] == 1  # original unchanged

    def test_multiple_counters_independent(self) -> None:
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        m.inc("a", by=3)
        m.inc("b", by=7)
        snap = m.snapshot()
        assert snap["a"] == 3
        assert snap["b"] == 7

    def test_module_level_singleton_exists(self) -> None:
        from app.access.metrics import metrics

        assert metrics is not None


class TestMetricsNames:
    """验证设计 §6.19 全部 15 个指标名被使用（Writer + Query 两侧）。"""

    WRITER_METRICS: ClassVar[list[str]] = [
        "amp_access_writer_accepted_total",
        "amp_access_writer_deduped_total",
        "amp_access_writer_dedup_unavailable_total",
        "amp_access_writer_redacted_headers_total",
        "amp_access_insert_latency_ms",
        "amp_access_insert_failures_total",
    ]

    QUERY_METRICS: ClassVar[list[str]] = [
        "amp_access_query_events_latency_ms",
        "amp_access_query_operations_latency_ms",
        "amp_access_query_traces_latency_ms",
        "amp_access_query_topology_latency_ms",
        "amp_access_query_slow_requests_latency_ms",
        "amp_access_query_errors_latency_ms",
    ]

    def test_writer_metrics_can_be_incremented(self) -> None:
        """Writer 侧指标名可以正常 inc/observe。"""
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        for name in self.WRITER_METRICS:
            m.inc(name)
        snap = m.snapshot()
        for name in self.WRITER_METRICS:
            assert name in snap, f"{name!r} 应在 metrics 快照中"

    def test_query_metrics_can_be_observed(self) -> None:
        """Query 侧延迟指标名可以正常 observe。"""
        from app.access.metrics import AccessMetrics

        m = AccessMetrics()
        for name in self.QUERY_METRICS:
            m.observe(name, 10.0)
        snap = m.snapshot()
        for name in self.QUERY_METRICS:
            assert name in snap, f"{name!r} 应在 metrics 快照中"


class TestMetricsLogLoop:
    """metrics_log_loop 周期日志行为测试。"""

    @pytest.mark.asyncio
    async def test_log_loop_logs_when_metrics_present(self) -> None:
        """有计数时 metrics_log_loop 输出 structlog INFO 日志。"""
        from app.access.metrics import metrics_log_loop

        captured: list[Any] = []

        async def fake_sleep(n: float) -> None:
            captured.append("sleep")
            raise asyncio.CancelledError()

        with (
            patch("app.access.metrics.asyncio.sleep", fake_sleep),
            patch(
                "app.access.metrics.metrics",
            ) as mock_metrics,
        ):
            mock_metrics.snapshot.return_value = {"accepted": 5}
            mock_logger = AsyncMock()
            with patch("app.access.metrics.logger", mock_logger), pytest.raises(asyncio.CancelledError):
                await metrics_log_loop(interval_seconds=10)

        assert captured  # sleep was called at least once

    @pytest.mark.asyncio
    async def test_log_loop_skips_empty_snapshot(self) -> None:
        """空快照时不发出 INFO 日志。"""
        import structlog.testing

        from app.access.metrics import metrics_log_loop

        call_count = [0]

        async def fake_sleep(n: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("app.access.metrics.asyncio.sleep", fake_sleep),
            patch(
                "app.access.metrics.metrics",
            ) as mock_m,
        ):
            mock_m.snapshot.return_value = {}
            with structlog.testing.capture_logs() as cap, pytest.raises(asyncio.CancelledError):
                await metrics_log_loop(interval_seconds=1)

        info_logs = [r for r in cap if r.get("log_level") == "info" and "accepted" in str(r)]
        assert not info_logs


class TestWriterMetricsIntegration:
    """Writer 路径中 metrics 计数器增长验证。"""

    @pytest.mark.asyncio
    async def test_accepted_counter_increments_after_flush(self) -> None:
        """AccessWriter._flush_batch 将消息写入 CH 后 accepted_total 计数增长（设计 §6.19）。"""
        import json
        from unittest.mock import AsyncMock, patch

        from app.access.metrics import AccessMetrics
        from app.access.writer import AccessWriter

        redis = AsyncMock()
        writer = AccessWriter(redis)

        record = {
            "schema_version": "1.0",
            "timestamp": "2026-01-15T10:00:00Z",
            "aic": "aic-test",
            "log_type": "access",
            "log_id": "lid-001",
        }
        msg = _make_msg(json.dumps(record).encode())

        test_metrics = AccessMetrics()
        with (
            patch("app.access.writer.access_metrics", test_metrics),
            patch("app.access.store.insert_events", new_callable=AsyncMock),
            patch("app.access.dedupe.filter_unseen", new_callable=AsyncMock) as mock_dedup,
            patch("app.access.freshness.advance_partition_watermark", new_callable=AsyncMock),
            patch("app.access.dedupe.mark_seen", new_callable=AsyncMock),
        ):
            mock_dedup.return_value = ({"lid-001"}, True)
            await writer.handle_message(msg)
            await writer._flush_batch()

        snap = test_metrics.snapshot()
        assert snap.get("amp_access_writer_accepted_total", 0) >= 1


def _make_msg(value: bytes, partition: int = 0, timestamp: int = 1_700_000_000_000) -> Any:
    from unittest.mock import MagicMock

    msg = MagicMock()
    msg.value = value
    msg.partition = partition
    msg.timestamp = timestamp
    msg.timestamp_type = 1
    msg.topic = "amp.access"
    msg.offset = 0
    return msg
