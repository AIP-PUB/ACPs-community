"""tests/unit/system/test_metrics.py — metrics.py 单元测试。"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

import pytest

from app.system.metrics import SystemMetrics, metrics, metrics_log_loop


class TestSystemMetrics:
    def test_inc_counter(self) -> None:
        m = SystemMetrics()
        m.inc("test_counter")
        assert m.snapshot()["test_counter"] == 1

    def test_inc_counter_multiple_times(self) -> None:
        m = SystemMetrics()
        m.inc("c", by=3)
        m.inc("c", by=2)
        assert m.snapshot()["c"] == 5

    def test_observe_histogram(self) -> None:
        m = SystemMetrics()
        m.observe("latency_ms", 100.0)
        m.observe("latency_ms", 200.0)
        snap = m.snapshot()
        assert snap["latency_ms_count"] == 2
        assert snap["latency_ms_sum_ms"] == 300.0
        assert snap["latency_ms_avg_ms"] == 150.0

    def test_set_gauge(self) -> None:
        m = SystemMetrics()
        m.set_gauge("lag_ms", 5000.0)
        assert m.snapshot()["lag_ms"] == 5000.0

    def test_snapshot_empty(self) -> None:
        m = SystemMetrics()
        assert m.snapshot() == {}

    def test_global_metrics_singleton(self) -> None:
        """进程级单例 metrics 存在且是 SystemMetrics 实例。"""
        assert isinstance(metrics, SystemMetrics)


class TestMetricsLogLoop:
    @pytest.mark.asyncio
    async def test_metrics_log_loop_emits_log(self) -> None:
        """metrics_log_loop 周期输出指标快照（mock sleep）。"""
        sleep_count = 0

        async def mock_sleep(seconds: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=mock_sleep), patch("app.system.metrics.logger") as mock_logger:
            with contextlib.suppress(asyncio.CancelledError):
                await metrics_log_loop(1)
        assert mock_logger.info.call_count >= 1
        call_args = mock_logger.info.call_args
        assert call_args.args[0] == "system.metrics"
