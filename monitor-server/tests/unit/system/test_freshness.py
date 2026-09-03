"""tests/unit/system/test_freshness.py — freshness.py 单元测试。"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from app.system.exception import ReadModelLaggingError
from app.system.freshness import (
    FreshnessView,
    advance_idle_partition,
    advance_partition_watermark,
    apply_degrade_policy,
    build_meta,
    evaluate_freshness,
    read_watermark,
)


def _make_redis() -> AsyncMock:
    store: dict[str, str] = {}
    sets: dict[str, set[str]] = {}

    async def get(key: str) -> str | None:
        return store.get(key)

    async def set_(key: str, val: str) -> None:
        store[key] = val

    async def sadd(key: str, member: str) -> None:
        sets.setdefault(key, set()).add(member)

    async def smembers(key: str) -> set[str]:
        return sets.get(key, set())

    async def mget(*keys: str) -> list[str | None]:
        return [store.get(k) for k in keys]

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=get)
    redis.set = AsyncMock(side_effect=set_)
    redis.sadd = AsyncMock(side_effect=sadd)
    redis.smembers = AsyncMock(side_effect=smembers)
    redis.mget = AsyncMock(side_effect=mget)
    return redis


class TestAdvancePartitionWatermark:
    """保守水位推进：candidate = batch_max_ts - reorder_margin；单调不回退、上限 now。"""

    @pytest.mark.asyncio
    async def test_watermark_less_than_batch_max_event_ts(self) -> None:
        """C-SYSTEM-DESIGN §2.4：保守水位 < batch_max_event_ts_ms（减去 reorder_margin）。"""
        redis = _make_redis()
        now_ms = int(time.time() * 1000)
        batch_max_ts_ms = now_ms - 5000
        reorder_margin_ms = 30000

        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=batch_max_ts_ms,
            now_ms=now_ms,
            reorder_margin_ms=reorder_margin_ms,
        )
        wm = await read_watermark(redis)
        # 保守水位 = min(now, max(prev, batch_max - margin)) = min(now, batch_max - margin)
        # batch_max - margin = now - 5000 - 30000 = now - 35000 < batch_max_ts
        assert wm is not None
        assert wm < batch_max_ts_ms

    @pytest.mark.asyncio
    async def test_monotonic_non_decreasing(self) -> None:
        """单调不回退：新水位不低于前一次水位。"""
        redis = _make_redis()
        now_ms = int(time.time() * 1000)

        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=now_ms - 5000,
            now_ms=now_ms,
            reorder_margin_ms=1000,
        )
        wm1 = await read_watermark(redis)
        assert wm1 is not None

        # 旧事件批次（更早的时间戳）不应降低水位
        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=now_ms - 100000,
            now_ms=now_ms,
            reorder_margin_ms=1000,
        )
        wm2 = await read_watermark(redis)
        assert wm2 is not None
        assert wm2 >= wm1

    @pytest.mark.asyncio
    async def test_watermark_capped_at_now(self) -> None:
        """水位上限为 now（防止未来事件推高水位）。"""
        redis = _make_redis()
        now_ms = int(time.time() * 1000)
        future_ts = now_ms + 60000  # 未来 1 分钟

        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=future_ts,
            now_ms=now_ms,
            reorder_margin_ms=0,
        )
        wm = await read_watermark(redis)
        assert wm is not None
        assert wm <= now_ms

    @pytest.mark.asyncio
    async def test_multiple_partitions_takes_min(self) -> None:
        """多分区水位取 min（保守，任一分区慢则整体慢）。"""
        redis = _make_redis()
        now_ms = int(time.time() * 1000)

        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=now_ms - 10000,
            now_ms=now_ms,
            reorder_margin_ms=1000,
        )
        await advance_partition_watermark(
            redis,
            partition_id=1,
            batch_max_event_ts_ms=now_ms - 50000,
            now_ms=now_ms,
            reorder_margin_ms=1000,
        )
        wm = await read_watermark(redis)
        assert wm is not None
        # min of two partitions; partition 1 is slower
        wm0_candidate = now_ms - 10000 - 1000
        wm1_candidate = now_ms - 50000 - 1000
        assert wm <= min(wm0_candidate, wm1_candidate) + 100  # small tolerance

    @pytest.mark.asyncio
    async def test_missing_partition_returns_none(self) -> None:
        """任一分区缺水位 → None（保守，C-SYSTEM-DESIGN §2.4）。"""
        redis = _make_redis()
        now_ms = int(time.time() * 1000)

        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=now_ms - 5000,
            now_ms=now_ms,
            reorder_margin_ms=1000,
        )
        # partition 1 未推进 → 整体水位 None
        # But partitions set only contains 0 currently. So read_watermark should work.
        # Force-add partition 1 to the set without setting its key
        await redis.sadd("amp:system:wm:ingest:partitions", "1")
        wm = await read_watermark(redis)
        assert wm is None


class TestAdvanceIdlePartition:
    @pytest.mark.asyncio
    async def test_idle_partition_converges_to_now_minus_margin(self) -> None:
        """空闲分区水位向 (now - reorder_margin) 收敛（防水位冻结）。"""
        redis = _make_redis()
        now_ms = int(time.time() * 1000)

        await advance_idle_partition(
            redis,
            partition_id=0,
            now_ms=now_ms,
            reorder_margin_ms=30000,
        )
        wm = await read_watermark(redis)
        assert wm is not None
        assert wm == now_ms - 30000


class TestEvaluateFreshness:
    @pytest.mark.asyncio
    async def test_lag_computed_correctly(self) -> None:
        redis = _make_redis()
        now_ms = int(time.time() * 1000)
        wm_ms = now_ms - 10000  # 10 seconds ago

        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=wm_ms + 30000,
            now_ms=now_ms,
            reorder_margin_ms=30000,
        )

        view = await evaluate_freshness(redis, now_ms=now_ms, lagging_threshold_ms=300000)
        assert view.data_freshness_at_ms is not None
        assert view.ingestion_lag_ms is not None
        assert not view.lagging

    @pytest.mark.asyncio
    async def test_lagging_when_lag_exceeds_threshold(self) -> None:
        redis = _make_redis()
        now_ms = int(time.time() * 1000)

        # 推一个很旧的水位
        await advance_partition_watermark(
            redis,
            partition_id=0,
            batch_max_event_ts_ms=now_ms - 400000,
            now_ms=now_ms,
            reorder_margin_ms=0,
        )

        view = await evaluate_freshness(redis, now_ms=now_ms, lagging_threshold_ms=300000)
        assert view.lagging is True

    @pytest.mark.asyncio
    async def test_no_watermark_is_lagging(self) -> None:
        """无水位（首次） → lagging=True（data_freshness_at_ms=None）。"""
        redis = _make_redis()
        view = await evaluate_freshness(redis, now_ms=int(time.time() * 1000), lagging_threshold_ms=300000)
        assert view.lagging is True
        assert view.data_freshness_at_ms is None
        assert view.ingestion_lag_ms is None


class TestApplyDegradePolicy:
    def test_not_lagging_returns_false(self) -> None:
        view = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=5000, lagging=False)
        assert apply_degrade_policy(view) is False

    def test_lagging_strict_raises_503(self) -> None:
        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        with pytest.raises(ReadModelLaggingError):
            apply_degrade_policy(view, strict_503=True)

    def test_lagging_non_strict_returns_true(self) -> None:
        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        assert apply_degrade_policy(view, strict_503=False) is True


class TestBuildMeta:
    def test_freshness_at_included_when_known(self) -> None:
        now_ms = int(time.time() * 1000)
        view = FreshnessView(data_freshness_at_ms=now_ms - 5000, ingestion_lag_ms=5000, lagging=False)
        meta = build_meta(view, now_ms=now_ms)
        assert meta.data_freshness_at is not None

    def test_freshness_at_excluded_when_none(self) -> None:
        now_ms = int(time.time() * 1000)
        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        meta = build_meta(view, now_ms=now_ms)
        assert meta.data_freshness_at is None

    def test_next_cursor_in_meta(self) -> None:
        now_ms = int(time.time() * 1000)
        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=False)
        meta = build_meta(view, now_ms=now_ms, next_cursor="cursor-abc")
        assert meta.next_cursor == "cursor-abc"

    def test_no_partial_data_fields(self) -> None:
        """system 不用 partialDataFields（事件检索非聚合，设计 §5.3）。"""
        now_ms = int(time.time() * 1000)
        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=False)
        meta = build_meta(view, now_ms=now_ms)
        assert meta.partial_data_fields is None
        assert meta.sample_coverage is None
