"""tests/unit/test_heartbeat_metrics.py — HeartbeatMetrics 与 metrics_log_loop 单元测试（Step 10）。

覆盖：
- HeartbeatMetrics.inc / gauge / observe_ms / snapshot 基本功能
- _sample_state_gauges：mock store → gauge 被更新（P1-5）
- Redis 异常 → WARNING 不传播
"""

from __future__ import annotations

import pytest


class TestHeartbeatMetrics:
    """HeartbeatMetrics 内存注册表基本行为。"""

    def _make(self) -> object:
        from app.heartbeat.metrics import HeartbeatMetrics

        return HeartbeatMetrics()

    def test_inc_defaults_to_1(self) -> None:
        """inc 默认增量为 1。"""
        m = self._make()
        m.inc("foo")  # type: ignore[attr-defined]
        assert m.snapshot()["foo"] == 1  # type: ignore[attr-defined]

    def test_inc_custom_value(self) -> None:
        """inc 可指定增量。"""
        m = self._make()
        m.inc("bar", 5)  # type: ignore[attr-defined]
        assert m.snapshot()["bar"] == 5  # type: ignore[attr-defined]

    def test_inc_accumulates(self) -> None:
        """多次 inc 累加。"""
        m = self._make()
        m.inc("cnt", 3)  # type: ignore[attr-defined]
        m.inc("cnt", 2)  # type: ignore[attr-defined]
        assert m.snapshot()["cnt"] == 5  # type: ignore[attr-defined]

    def test_gauge_sets_value(self) -> None:
        """gauge 设置（覆盖）值。"""
        m = self._make()
        m.gauge("g", 100)  # type: ignore[attr-defined]
        m.gauge("g", 42)  # type: ignore[attr-defined]
        assert m.snapshot()["g"] == 42  # type: ignore[attr-defined]

    def test_observe_ms_accumulates(self) -> None:
        """observe_ms 累加到 _ms_total 键。"""
        m = self._make()
        m.observe_ms("latency", 100)  # type: ignore[attr-defined]
        m.observe_ms("latency", 50)  # type: ignore[attr-defined]
        snap = m.snapshot()  # type: ignore[attr-defined]
        assert snap.get("latency_ms_total", 0) == 150

    def test_inc_with_shard_label(self) -> None:
        """inc 带 shard 标签 → 键名为 'name{shard=<shard>}'。"""
        m = self._make()
        m.inc("writer_accepted", shard="hb-000")  # type: ignore[attr-defined]
        snap = m.snapshot()  # type: ignore[attr-defined]
        assert "writer_accepted{shard=hb-000}" in snap

    def test_snapshot_returns_dict_copy(self) -> None:
        """snapshot() 返回独立 dict，不影响内部状态。"""
        m = self._make()
        m.inc("x", 1)  # type: ignore[attr-defined]
        snap1 = m.snapshot()  # type: ignore[attr-defined]
        snap1["x"] = 999
        snap2 = m.snapshot()  # type: ignore[attr-defined]
        assert snap2["x"] == 1

    def test_module_singleton_exists(self) -> None:
        """模块级 metrics 单例可直接导入。"""
        from app.heartbeat.metrics import metrics

        metrics.inc("test_import_ok")
        assert metrics.snapshot()["test_import_ok"] >= 1


class TestSampleStateGauges:
    """_sample_state_gauges：从 store 读取 Redis 当前态并更新 gauge（P1-5）。"""

    @pytest.mark.asyncio
    async def test_updates_four_gauges(self) -> None:
        """成功采样时更新四个 gauge。"""
        from unittest.mock import AsyncMock, patch

        from app.heartbeat.metrics import HeartbeatMetrics, _sample_state_gauges

        m = HeartbeatMetrics()
        with (
            patch("app.heartbeat.metrics.metrics", m),
            patch("app.heartbeat.metrics.zcard", AsyncMock(return_value=10)),
            patch("app.heartbeat.metrics.zcount_score_at_least", AsyncMock(return_value=7)),
            patch("app.heartbeat.metrics.redis_now_ms", AsyncMock(return_value=1_700_000_000_000)),
        ):
            await _sample_state_gauges(None)  # type: ignore[arg-type]

        snap = m.snapshot()
        assert snap.get("amp_heartbeat_latest_rows", 0) > 0
        assert snap.get("amp_heartbeat_liveness_zset_size", 0) > 0
        assert snap.get("amp_heartbeat_alive_rows", 0) > 0
        assert "amp_heartbeat_silent_rows" in snap

    @pytest.mark.asyncio
    async def test_redis_exception_does_not_propagate(self) -> None:
        """Redis 异常只打 WARNING，不向外抛出。"""
        from unittest.mock import AsyncMock, patch

        from app.heartbeat.metrics import _sample_state_gauges

        with patch("app.heartbeat.metrics.zcard", AsyncMock(side_effect=Exception("redis down"))):
            await _sample_state_gauges(None)  # type: ignore[arg-type]
        # 不抛异常即通过
