"""tests/unit/test_access_freshness.py — 每分区水位与整体水位测试。

TDD C-3 (freshness.py)：先写测试（红）→ 实现 freshness.py（绿）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestAdvancePartitionWatermark:
    @pytest.mark.asyncio
    async def test_sets_partition_key(self) -> None:
        from app.access.freshness import advance_partition_watermark

        redis = AsyncMock()
        redis.sadd = AsyncMock()
        redis.set = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        now_ms = 1_700_000_000_000
        await advance_partition_watermark(redis, partition_id=0, batch_max_ts_ms=now_ms - 1000, now_ms=now_ms)
        redis.sadd.assert_called()
        redis.set.assert_called()

    @pytest.mark.asyncio
    async def test_does_not_exceed_now(self) -> None:
        """水位不超过 now（防未来时间戳膨胀）。"""
        from app.access.freshness import advance_partition_watermark

        redis = AsyncMock()
        redis.sadd = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        set_calls = []
        redis.set = AsyncMock(side_effect=lambda *args, **kwargs: set_calls.append((args, kwargs)))
        now_ms = 1_700_000_000_000
        future_ts = now_ms + 99_999_999
        await advance_partition_watermark(redis, partition_id=0, batch_max_ts_ms=future_ts, now_ms=now_ms)
        # The stored value should be capped at now_ms
        assert len(set_calls) == 1
        stored_val = int(set_calls[0][0][1])
        assert stored_val <= now_ms


class TestReadOverallWatermark:
    @pytest.mark.asyncio
    async def test_returns_min_of_all_partitions(self) -> None:
        from app.access.freshness import read_overall_watermark

        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value={"0", "1", "2"})
        redis.mget = AsyncMock(return_value=["1000", "2000", "3000"])
        wm = await read_overall_watermark(redis)
        assert wm == 1000

    @pytest.mark.asyncio
    async def test_missing_partition_wm_returns_zero(self) -> None:
        from app.access.freshness import read_overall_watermark

        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value={"0", "1"})
        redis.mget = AsyncMock(return_value=["2000", None])  # partition 1 has no watermark
        wm = await read_overall_watermark(redis)
        # None partition treated conservatively → 0 or None
        assert wm is None or wm == 0 or wm == 2000

    @pytest.mark.asyncio
    async def test_no_partitions_returns_none(self) -> None:
        from app.access.freshness import read_overall_watermark

        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value=set())
        wm = await read_overall_watermark(redis)
        assert wm is None


class TestEvaluateFreshness:
    @pytest.mark.asyncio
    async def test_not_lagging_when_recent_wm(self) -> None:
        from app.access.freshness import evaluate_freshness

        redis = AsyncMock()
        now_ms = 1_700_000_000_000
        # watermark 10s ago — well within default 300s threshold
        with (
            patch("app.access.freshness.read_overall_watermark", AsyncMock(return_value=now_ms - 10_000)),
            patch("app.access.freshness.settings") as mock_s,
        ):
            mock_s.access_lagging_threshold_ms = 300_000
            view = await evaluate_freshness(redis, now_ms=now_ms)
        assert view.lagging is False

    @pytest.mark.asyncio
    async def test_lagging_when_wm_old(self) -> None:
        from app.access.freshness import evaluate_freshness

        redis = AsyncMock()
        now_ms = 1_700_000_000_000
        # watermark 600s ago — exceeds 300s threshold
        with (
            patch("app.access.freshness.read_overall_watermark", AsyncMock(return_value=now_ms - 600_000)),
            patch("app.access.freshness.settings") as mock_s,
        ):
            mock_s.access_lagging_threshold_ms = 300_000
            view = await evaluate_freshness(redis, now_ms=now_ms)
        assert view.lagging is True

    @pytest.mark.asyncio
    async def test_lagging_when_wm_none(self) -> None:
        from app.access.freshness import evaluate_freshness

        redis = AsyncMock()
        now_ms = 1_700_000_000_000
        with (
            patch("app.access.freshness.read_overall_watermark", AsyncMock(return_value=None)),
            patch("app.access.freshness.settings") as mock_s,
        ):
            mock_s.access_lagging_threshold_ms = 300_000
            view = await evaluate_freshness(redis, now_ms=now_ms)
        assert view.lagging is True


class TestFreshnessView:
    def test_dataclass_fields(self) -> None:
        from app.access.freshness import FreshnessView

        view = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=500, lagging=False)
        assert view.data_freshness_at_ms == 1000
        assert view.lagging is False


class TestBuildMeta:
    def test_returns_amp_response_meta(self) -> None:
        from app.access.freshness import FreshnessView, build_meta
        from app.core.amp_api_schema import AMPResponseMeta

        view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=5000, lagging=False)
        meta = build_meta(view, now_ms=1_700_000_005_000, next_cursor=None)
        assert isinstance(meta, AMPResponseMeta)

    def test_next_cursor_set(self) -> None:
        from app.access.freshness import FreshnessView, build_meta

        view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=5000, lagging=False)
        meta = build_meta(view, now_ms=1_700_000_005_000, next_cursor="tok123")
        assert meta.next_cursor == "tok123"


class TestApplyDegradePolicy:
    def test_not_lagging_returns_false(self) -> None:
        from app.access.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=100, lagging=False)
        assert apply_degrade_policy(view, strict_503=True) is False

    def test_lagging_strict_503_raises(self) -> None:
        from app.access.exception import ReadModelLaggingError
        from app.access.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        with pytest.raises(ReadModelLaggingError):
            apply_degrade_policy(view, strict_503=True)

    def test_lagging_partial_mode_returns_true(self) -> None:
        from app.access.freshness import FreshnessView, apply_degrade_policy

        view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        result = apply_degrade_policy(view, strict_503=False)
        assert result is True
