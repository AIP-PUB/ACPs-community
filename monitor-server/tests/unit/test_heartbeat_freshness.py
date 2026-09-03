"""tests/unit/test_heartbeat_freshness.py — FreshnessView 与 evaluate_freshness 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

NOW_MS = 1_700_000_000_000


class TestEvaluateFreshness:
    """evaluate_freshness 读模型新鲜度评估。"""

    @pytest.mark.asyncio
    async def test_all_partitions_fresh_no_lagging(self) -> None:
        """所有 watermark 均在 stale_threshold 内，lagging_count=0。"""
        from app.heartbeat.freshness import evaluate_freshness

        fresh_wm = NOW_MS - 500  # 500ms 前

        with patch("app.heartbeat.freshness.read_watermarks", AsyncMock(return_value={0: (fresh_wm, NOW_MS)})):
            view = await evaluate_freshness(None, partitions=[0], now_ms=NOW_MS)  # type: ignore[arg-type]

        assert view.lagging_partition_count == 0
        assert view.all_unknown is False
        assert view.min_watermark_ms == fresh_wm

    @pytest.mark.asyncio
    async def test_missing_partition_counts_as_lagging(self) -> None:
        """请求的 partition 在 watermarks 中缺失时，lagging_count 加 1。"""
        from app.heartbeat.freshness import evaluate_freshness

        fresh_wm = NOW_MS - 500

        with patch("app.heartbeat.freshness.read_watermarks", AsyncMock(return_value={0: (fresh_wm, NOW_MS)})):
            view = await evaluate_freshness(None, partitions=[0, 1], now_ms=NOW_MS)  # type: ignore[arg-type]

        assert view.lagging_partition_count == 1
        assert view.all_unknown is False

    @pytest.mark.asyncio
    async def test_all_unknown_when_no_watermarks(self) -> None:
        """watermarks 为空时，all_unknown=True，min_watermark_ms=now_ms。"""
        from app.heartbeat.freshness import evaluate_freshness

        with patch("app.heartbeat.freshness.read_watermarks", AsyncMock(return_value={})):
            view = await evaluate_freshness(None, partitions=[0], now_ms=NOW_MS)  # type: ignore[arg-type]

        assert view.all_unknown is True
        assert view.min_watermark_ms == NOW_MS
        assert view.lagging_partition_count == 1

    @pytest.mark.asyncio
    async def test_min_watermark_is_minimum(self) -> None:
        """min_watermark_ms = 所有已知 watermark 的最小值。"""
        from app.heartbeat.freshness import evaluate_freshness

        with patch(
            "app.heartbeat.freshness.read_watermarks",
            AsyncMock(return_value={0: (NOW_MS - 1000, NOW_MS), 1: (NOW_MS - 500, NOW_MS)}),
        ):
            view = await evaluate_freshness(None, partitions=[0, 1], now_ms=NOW_MS)  # type: ignore[arg-type]

        assert view.min_watermark_ms == NOW_MS - 1000

    @pytest.mark.asyncio
    async def test_stale_watermark_counts_as_lagging(self) -> None:
        """watermark 存在但早于 stale_threshold，计入 lagging（C-QUERY-5）。"""
        from app.core.config import settings
        from app.heartbeat.freshness import evaluate_freshness

        stale_wm = NOW_MS - settings.heartbeat_writer_watermark_stale_after_ms - 1_000

        with patch("app.heartbeat.freshness.read_watermarks", AsyncMock(return_value={0: (stale_wm, NOW_MS)})):
            view = await evaluate_freshness(None, partitions=[0], now_ms=NOW_MS)  # type: ignore[arg-type]

        assert view.lagging_partition_count == 1
        assert view.all_unknown is False

    @pytest.mark.asyncio
    async def test_no_lagging_when_partitions_is_none(self) -> None:
        """partitions=None（全局查询）时，lagging_count=0（无需检查具体分区）。"""
        from app.heartbeat.freshness import evaluate_freshness

        with patch("app.heartbeat.freshness.read_watermarks", AsyncMock(return_value={0: (NOW_MS - 100, NOW_MS)})):
            view = await evaluate_freshness(None, partitions=None, now_ms=NOW_MS)  # type: ignore[arg-type]

        assert view.lagging_partition_count == 0


class TestApplyDegradePolicy:
    """apply_degrade_policy 降级策略。"""

    def test_all_unknown_always_degrades(self) -> None:
        """all_unknown=True 时必然降级（强制 503）。"""
        from app.heartbeat.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(min_watermark_ms=NOW_MS, lagging_partition_count=1, all_unknown=True, watermarks={})
        assert apply_degrade_policy(view, strict_503=False) is True

    def test_strict_503_with_lagging_degrades(self) -> None:
        """strict_503=True 且有 lagging 分区时降级。"""
        from app.heartbeat.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(
            min_watermark_ms=NOW_MS - 100,
            lagging_partition_count=1,
            all_unknown=False,
            watermarks={0: NOW_MS - 100},
        )
        assert apply_degrade_policy(view, strict_503=True) is True

    def test_partial_ok_when_not_strict(self) -> None:
        """strict_503=False 时，lagging > 0 不触发 503（partial 响应）。"""
        from app.heartbeat.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(
            min_watermark_ms=NOW_MS - 100,
            lagging_partition_count=1,
            all_unknown=False,
            watermarks={0: NOW_MS - 100},
        )
        assert apply_degrade_policy(view, strict_503=False) is False

    def test_no_lagging_no_degrade(self) -> None:
        """lagging=0 时不降级。"""
        from app.heartbeat.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(
            min_watermark_ms=NOW_MS - 100,
            lagging_partition_count=0,
            all_unknown=False,
            watermarks={0: NOW_MS - 100},
        )
        assert apply_degrade_policy(view, strict_503=True) is False


class TestPointLookupPartitions:
    """point_lookup_partitions 单点查询分区推算。"""

    def test_returns_list_with_single_partition(self) -> None:
        """返回单元素列表。"""
        from app.heartbeat.freshness import point_lookup_partitions

        result = point_lookup_partitions("aic-test-001")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_same_aic_always_same_partition(self) -> None:
        """幂等：同一 AIC 每次返回相同 partition。"""
        from app.heartbeat.freshness import point_lookup_partitions

        p1 = point_lookup_partitions("aic-stable-001")
        p2 = point_lookup_partitions("aic-stable-001")
        assert p1 == p2
