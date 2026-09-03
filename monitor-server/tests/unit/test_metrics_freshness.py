"""tests/unit/test_metrics_freshness.py — freshness 纯函数与降级策略单元测试（Step 4）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.metrics.exception import ReadModelLaggingError
from app.metrics.freshness import (
    FreshnessView,
    apply_degrade_policy,
    build_meta,
    evaluate_freshness,
    read_watermark,
)

# ── read_watermark ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_watermark_returns_int_when_present() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1700000000000")
    result = await read_watermark(redis)
    assert result == 1700000000000


@pytest.mark.asyncio
async def test_read_watermark_returns_none_when_missing() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    result = await read_watermark(redis)
    assert result is None


@pytest.mark.asyncio
async def test_read_watermark_returns_none_on_invalid_value() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"not-a-number")
    result = await read_watermark(redis)
    assert result is None


# ── evaluate_freshness ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_freshness_no_watermark_is_lagging() -> None:
    """水位缺失 → lagging=True（强制 503 语义）。"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)

    view = await evaluate_freshness(redis, now_ms=1_700_000_000_000)
    assert view.lagging is True
    assert view.data_freshness_at_ms is None
    assert view.ingestion_lag_ms is None


@pytest.mark.asyncio
async def test_evaluate_freshness_fresh_data() -> None:
    """水位在阈值内 → lagging=False。"""
    now_ms = 1_700_000_000_000
    wm_ms = now_ms - 1000  # 1s lag（默认阈值 150s）

    redis = AsyncMock()
    redis.get = AsyncMock(return_value=str(wm_ms).encode())

    view = await evaluate_freshness(redis, now_ms=now_ms)
    assert view.lagging is False
    assert view.ingestion_lag_ms == 1000
    assert view.data_freshness_at_ms == wm_ms


@pytest.mark.asyncio
async def test_evaluate_freshness_stale_data_exceeds_threshold() -> None:
    """lag > lagging_threshold_ms → lagging=True。"""
    now_ms = 1_700_000_000_000
    wm_ms = now_ms - 200_000  # 200s lag > 默认 150s 阈值

    redis = AsyncMock()
    redis.get = AsyncMock(return_value=str(wm_ms).encode())

    view = await evaluate_freshness(redis, now_ms=now_ms)
    assert view.lagging is True
    assert view.ingestion_lag_ms == 200_000


# ── apply_degrade_policy ──────────────────────────────────────────────────────


def test_apply_degrade_policy_no_watermark_raises() -> None:
    """watermark 未知 → 一律 raise ReadModelLaggingError。"""
    view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
    with pytest.raises(ReadModelLaggingError):
        apply_degrade_policy(view)


def test_apply_degrade_policy_not_lagging_returns_false() -> None:
    """数据新鲜 → 返回 False（无需降级）。"""
    view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=100, lagging=False)
    result = apply_degrade_policy(view)
    assert result is False


def test_apply_degrade_policy_lagging_503_mode_raises() -> None:
    """lagging=True + mode=503 → raise ReadModelLaggingError。"""
    view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=200_000, lagging=True)
    with patch("app.core.config.get_settings") as mock_settings:
        mock_settings.return_value.metrics_lagging_response_mode = "503"
        with pytest.raises(ReadModelLaggingError):
            apply_degrade_policy(view)


def test_apply_degrade_policy_lagging_partial_mode_returns_true() -> None:
    """lagging=True + mode=partial → 返回 True（partial 标记）。"""
    view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=200_000, lagging=True)
    with patch("app.core.config.get_settings") as mock_settings:
        mock_settings.return_value.metrics_lagging_response_mode = "partial"
        result = apply_degrade_policy(view)
    assert result is True


def test_apply_degrade_policy_strict_503_overrides_partial() -> None:
    """strict_503=True 无论 mode → 一律 raise。"""
    view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=200_000, lagging=True)
    with patch("app.core.config.get_settings") as mock_settings:
        mock_settings.return_value.metrics_lagging_response_mode = "partial"
        with pytest.raises(ReadModelLaggingError):
            apply_degrade_policy(view, strict_503=True)


# ── build_meta ────────────────────────────────────────────────────────────────


def test_build_meta_basic() -> None:
    """build_meta 构造 AMPResponseMeta 四件套。"""
    view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=1000, lagging=False)
    meta = build_meta(view, now_ms=1_700_000_001_000)

    assert meta.data_freshness_at is not None
    assert meta.ingestion_lag_ms == 1000
    assert meta.next_cursor is None
    assert meta.partial is None


def test_build_meta_with_cursor_and_partial() -> None:
    view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=500, lagging=False)
    meta = build_meta(view, now_ms=1_700_000_000_500, next_cursor="abc123", partial=True, elapsed_ms=42)

    assert meta.next_cursor == "abc123"
    assert meta.partial is True
    assert meta.elapsed_ms == 42


def test_build_meta_raises_when_no_watermark() -> None:
    """data_freshness_at_ms=None → ValueError（调用方需先 apply_degrade_policy）。"""
    view = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
    with pytest.raises(ValueError):
        build_meta(view, now_ms=1_700_000_000_000)
