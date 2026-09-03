"""单元测试：C-2b freshness.py — 四独立水位与新鲜度评估（Redis mock）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.message.exception import ReadModelLaggingError
from app.message.freshness import (
    WM_INGEST_PREFIX,
    WM_LIFECYCLE,
    WM_STATE,
    WM_THROUGHPUT,
    FreshnessView,
    advance_partition_watermark,
    apply_degrade_policy,
    build_meta,
    evaluate_freshness,
    freshness_headers,
    read_compaction_watermark,
    read_events_watermark,
    read_state_watermark,
    set_compaction_watermark,
    set_state_watermark,
)

NOW_MS = 1_800_000_000_000


def _make_redis(**returns: object) -> MagicMock:
    redis = MagicMock()
    for method, ret in returns.items():
        setattr(redis, method, AsyncMock(return_value=ret))
    return redis


# ── 常量检查 ──────────────────────────────────────────────────────────────────


class TestWatermarkKeys:
    def test_ingest_prefix(self) -> None:
        assert "amp:message:wm:ingest:" in WM_INGEST_PREFIX

    def test_lifecycle_key(self) -> None:
        assert "lifecycle" in WM_LIFECYCLE

    def test_throughput_key(self) -> None:
        assert "throughput" in WM_THROUGHPUT

    def test_state_key(self) -> None:
        assert "state" in WM_STATE


# ── advance_partition_watermark ────────────────────────────────────────────────


class TestAdvancePartitionWatermark:
    @pytest.mark.asyncio
    async def test_sets_new_watermark(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"1000000")
        redis.set = AsyncMock()
        redis.sadd = AsyncMock()
        await advance_partition_watermark(redis, partition_id=0, batch_max_ts_ms=2_000_000, now_ms=3_000_000)
        assert redis.set.called

    @pytest.mark.asyncio
    async def test_does_not_exceed_now(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.sadd = AsyncMock()
        await advance_partition_watermark(redis, partition_id=0, batch_max_ts_ms=9_999_999_999, now_ms=NOW_MS)
        written = int(redis.set.call_args[0][1])
        assert written <= NOW_MS

    @pytest.mark.asyncio
    async def test_monotonically_increasing(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=str(NOW_MS).encode())
        redis.set = AsyncMock()
        redis.sadd = AsyncMock()
        await advance_partition_watermark(redis, partition_id=0, batch_max_ts_ms=100, now_ms=NOW_MS)
        written = int(redis.set.call_args[0][1])
        assert written >= NOW_MS - 1000


# ── read_events_watermark ─────────────────────────────────────────────────────


class TestReadEventsWatermark:
    @pytest.mark.asyncio
    async def test_no_partitions_returns_none(self) -> None:
        redis = MagicMock()
        redis.smembers = AsyncMock(return_value=set())
        result = await read_events_watermark(redis)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_min_of_all_partitions(self) -> None:
        redis = MagicMock()
        redis.smembers = AsyncMock(return_value={b"0", b"1"})
        redis.mget = AsyncMock(return_value=[b"1000", b"500"])
        result = await read_events_watermark(redis)
        assert result == 500

    @pytest.mark.asyncio
    async def test_any_partition_missing_returns_none(self) -> None:
        redis = MagicMock()
        redis.smembers = AsyncMock(return_value={b"0", b"1"})
        redis.mget = AsyncMock(return_value=[b"1000", None])
        result = await read_events_watermark(redis)
        assert result is None


# ── compaction watermark ──────────────────────────────────────────────────────


class TestCompactionWatermark:
    @pytest.mark.asyncio
    async def test_read_lifecycle_watermark_none(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        result = await read_compaction_watermark(redis, kind="lifecycle")
        assert result is None

    @pytest.mark.asyncio
    async def test_read_lifecycle_watermark_value(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"5000000")
        result = await read_compaction_watermark(redis, kind="lifecycle")
        assert result == 5_000_000

    @pytest.mark.asyncio
    async def test_set_compaction_watermark(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        await set_compaction_watermark(redis, kind="throughput", watermark_ms=12345)
        redis.set.assert_called_once()
        args = redis.set.call_args[0]
        assert "12345" in str(args[1])

    @pytest.mark.asyncio
    async def test_set_compaction_watermark_monotonic(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"9999999")
        redis.set = AsyncMock()
        await set_compaction_watermark(redis, kind="lifecycle", watermark_ms=100)
        written = int(redis.set.call_args[0][1])
        assert written == 9_999_999


# ── state watermark ───────────────────────────────────────────────────────────


class TestStateWatermark:
    @pytest.mark.asyncio
    async def test_read_state_watermark_none(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        result = await read_state_watermark(redis)
        assert result is None

    @pytest.mark.asyncio
    async def test_set_state_watermark(self) -> None:
        redis = MagicMock()
        redis.set = AsyncMock()
        await set_state_watermark(redis, captured_at_ms=99999)
        redis.set.assert_called_once()


# ── evaluate_freshness ────────────────────────────────────────────────────────


class TestEvaluateFreshness:
    @pytest.mark.asyncio
    async def test_lagging_when_watermark_none(self) -> None:
        redis = MagicMock()
        redis.smembers = AsyncMock(return_value=set())
        with patch("app.message.freshness.settings") as mock_settings:
            mock_settings.message_lagging_threshold_ms = 300_000
            fv = await evaluate_freshness(redis, read_model="events", now_ms=NOW_MS)
        assert fv.lagging is True
        assert fv.data_freshness_at_ms is None

    @pytest.mark.asyncio
    async def test_not_lagging_fresh_watermark(self) -> None:
        redis = MagicMock()
        redis.smembers = AsyncMock(return_value={b"0"})
        redis.mget = AsyncMock(return_value=[str(NOW_MS - 100_000).encode()])
        with patch("app.message.freshness.settings") as mock_settings:
            mock_settings.message_lagging_threshold_ms = 300_000
            fv = await evaluate_freshness(redis, read_model="events", now_ms=NOW_MS)
        assert fv.lagging is False
        assert fv.data_freshness_at_ms == NOW_MS - 100_000

    @pytest.mark.asyncio
    async def test_lagging_stale_watermark(self) -> None:
        redis = MagicMock()
        redis.smembers = AsyncMock(return_value={b"0"})
        redis.mget = AsyncMock(return_value=[str(NOW_MS - 600_000).encode()])
        with patch("app.message.freshness.settings") as mock_settings:
            mock_settings.message_lagging_threshold_ms = 300_000
            fv = await evaluate_freshness(redis, read_model="events", now_ms=NOW_MS)
        assert fv.lagging is True

    @pytest.mark.asyncio
    async def test_lifecycle_uses_compaction_watermark(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=str(NOW_MS - 100_000).encode())
        with patch("app.message.freshness.settings") as mock_settings:
            mock_settings.message_lagging_threshold_ms = 300_000
            fv = await evaluate_freshness(redis, read_model="lifecycle", now_ms=NOW_MS)
        assert fv.data_freshness_at_ms == NOW_MS - 100_000


# ── apply_degrade_policy ──────────────────────────────────────────────────────


class TestApplyDegradePolicy:
    def test_not_lagging_returns_false(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=100, lagging=False)
        assert apply_degrade_policy(fv) is False

    def test_lagging_strict_false_returns_true(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        assert apply_degrade_policy(fv, strict_503=False) is True

    def test_lagging_strict_true_raises(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        with pytest.raises(ReadModelLaggingError):
            apply_degrade_policy(fv, strict_503=True)


# ── build_meta ────────────────────────────────────────────────────────────────


class TestBuildMeta:
    def test_freshness_at_iso_when_watermark_set(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=1_800_000_000_000, ingestion_lag_ms=5000, lagging=False)
        meta = build_meta(fv, now_ms=NOW_MS)
        assert meta.data_freshness_at is not None
        assert "2027" in meta.data_freshness_at or "20" in meta.data_freshness_at

    def test_freshness_at_none_when_watermark_none(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        meta = build_meta(fv, now_ms=NOW_MS)
        assert meta.data_freshness_at is None

    def test_next_cursor_propagated(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=100, lagging=False)
        meta = build_meta(fv, now_ms=NOW_MS, next_cursor="abc")
        assert meta.next_cursor == "abc"

    def test_partial_propagated(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=100, lagging=False)
        meta = build_meta(fv, now_ms=NOW_MS, partial=True)
        assert meta.partial is True

    def test_partial_data_fields_propagated(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=1000, ingestion_lag_ms=100, lagging=False)
        meta = build_meta(fv, now_ms=NOW_MS, partial_data_fields=["visible_messages"])
        assert meta.partial_data_fields == ["visible_messages"]


# ── freshness_headers ─────────────────────────────────────────────────────────


class TestFreshnessHeaders:
    def test_headers_when_watermark_set(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=1_800_000_000_000, ingestion_lag_ms=1000, lagging=False)
        headers = freshness_headers(fv)
        assert "AMP-Data-Freshness-At" in headers
        assert "AMP-Ingestion-Lag-Ms" in headers

    def test_empty_when_watermark_none(self) -> None:
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        headers = freshness_headers(fv)
        assert headers == {}
