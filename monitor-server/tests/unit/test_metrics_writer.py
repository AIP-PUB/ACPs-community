"""tests/unit/test_metrics_writer.py — MetricsWriter 单元测试（Step 5）。

使用 mock 替换 tsdb / dedupe / snapshot_cache / freshness，隔离 Writer 逻辑。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.metrics.exception import RemoteWriteError, UntimedMetricsError
from app.metrics.metrics import MetricsMetrics
from app.metrics.writer import MetricsWriter
from tests.support.factory import make_metrics_log_record

# ── 辅助：构造伪消息 ──────────────────────────────────────────────────────────


def _make_msg(
    value: dict[str, Any] | None = None,
    *,
    timestamp: int | None = 1_700_000_000_000,
    timestamp_type: int = 1,
    offset: int = 0,
) -> MagicMock:
    msg = MagicMock()
    if value is None:
        value = make_metrics_log_record()
    msg.value = json.dumps(value).encode()
    msg.timestamp = timestamp
    msg.timestamp_type = timestamp_type
    msg.offset = offset
    return msg


def _make_writer() -> MetricsWriter:
    redis = AsyncMock()
    with patch("app.metrics.writer.MetricsWriter.__init__", lambda self, redis: None):
        writer = MetricsWriter.__new__(MetricsWriter)
        writer._redis = redis
        writer._pending = []
        writer._last_flush_monotonic = 0.0
        writer._running = True
        writer._consumer = None
    return writer


# ── log_type 过滤 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_type_filter_non_metrics_ignored() -> None:
    """log_type != 'metrics' → handle_message 不追加 _pending。"""
    writer = _make_writer()
    non_metrics_record = make_metrics_log_record()
    non_metrics_record["log_type"] = "audit"
    msg = _make_msg(non_metrics_record)

    await writer.handle_message(msg)
    assert writer._pending == []


@pytest.mark.asyncio
async def test_log_type_metrics_is_accepted() -> None:
    """log_type == 'metrics' → _pending 增加一条。"""
    writer = _make_writer()
    msg = _make_msg()

    await writer.handle_message(msg)
    assert len(writer._pending) == 1
    assert writer._pending[0].aic == "test-aic-001"


# ── observed_at 提取 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observed_at_uses_kafka_timestamp_when_present() -> None:
    """message.timestamp 有效 → observed_at_ms = message.timestamp（LogAppendTime 优先）。"""
    writer = _make_writer()
    ts = 1_700_000_001_234
    msg = _make_msg(timestamp=ts)

    await writer.handle_message(msg)
    assert writer._pending[0].observed_at_ms == ts


@pytest.mark.asyncio
async def test_observed_at_fallback_to_observed_timestamp() -> None:
    """message.timestamp 为 0 → 退而求其次用 observed_timestamp。"""
    writer = _make_writer()
    dt = datetime(2023, 11, 14, 12, 0, 0, tzinfo=UTC)
    iso = dt.isoformat()
    expected_ms = int(dt.timestamp() * 1000)
    record = make_metrics_log_record(observed_timestamp=iso)
    msg = _make_msg(record, timestamp=0, timestamp_type=0)

    await writer.handle_message(msg)
    assert writer._pending[0].observed_at_ms == expected_ms


@pytest.mark.asyncio
async def test_observed_at_missing_raises_untimed_error() -> None:
    """message.timestamp=0 + no observed_timestamp → UntimedMetricsError。"""
    writer = _make_writer()
    record = make_metrics_log_record(observed_timestamp=None)
    msg = _make_msg(record, timestamp=0, timestamp_type=0)

    with pytest.raises(UntimedMetricsError):
        await writer.handle_message(msg)


# ── _process_with_retry 覆写 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_untimed_goes_to_dlq_no_retry() -> None:
    """UntimedMetricsError → _process_with_retry 返回 False（不重试）。"""
    writer = _make_writer()
    record = make_metrics_log_record(observed_timestamp=None)
    msg = _make_msg(record, timestamp=0, timestamp_type=0)

    result = await writer._process_with_retry(msg)
    assert result is False


@pytest.mark.asyncio
async def test_json_decode_error_goes_to_dlq() -> None:
    """JSON 解析失败 → _process_with_retry 返回 False。"""
    writer = _make_writer()
    msg = MagicMock()
    msg.value = b"not-valid-json"
    msg.timestamp = 1_700_000_000_000
    msg.timestamp_type = 1
    msg.offset = 0

    result = await writer._process_with_retry(msg)
    assert result is False


# ── _flush_batch 去重与写入 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedupe_before_remote_write_filters_duplicates() -> None:
    """重复 log_id 的样本不进入 remote_write（C-METRIC-WRITE-4）。"""
    writer = _make_writer()
    msg = _make_msg()
    await writer.handle_message(msg)
    prepared = list(writer._pending)

    with (
        patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=set())),
        patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock()) as mock_rw,
        patch("app.metrics.writer.snapshot_cache.upsert_snapshot", new=AsyncMock()),
        patch("app.metrics.writer.advance_watermark", new=AsyncMock()),
    ):
        result = await writer._flush_batch(prepared)

    mock_rw.assert_not_called()
    # 全是重复投递 → kept 为空 → 返回 True（不重写 VM）
    assert result is True


@pytest.mark.asyncio
async def test_flush_success_calls_commit_and_cache() -> None:
    """remote_write 成功 → upsert_snapshot & advance_watermark 被调用（C-METRIC-WRITE-1）。"""
    writer = _make_writer()
    msg = _make_msg()
    await writer.handle_message(msg)
    prepared = list(writer._pending)
    log_ids = {p.log_id for p in prepared}

    mock_upsert = AsyncMock()
    mock_advance = AsyncMock()

    with (
        patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=log_ids)),
        patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock()),
        patch("app.metrics.writer.snapshot_cache.upsert_snapshot", new=mock_upsert),
        patch("app.metrics.writer.advance_watermark", new=mock_advance),
    ):
        result = await writer._flush_batch(prepared)

    assert result is True
    mock_upsert.assert_called_once()
    mock_advance.assert_called_once()


@pytest.mark.asyncio
async def test_flush_failure_returns_false_and_rolls_back_claim() -> None:
    """remote_write 失败 → 返回 False，release_log_ids 被调用（回滚去重占用）。"""
    writer = _make_writer()
    msg = _make_msg()
    await writer.handle_message(msg)
    prepared = list(writer._pending)
    log_ids = {p.log_id for p in prepared}

    mock_release = AsyncMock()
    mock_upsert = AsyncMock()
    mock_advance = AsyncMock()

    with (
        patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=log_ids)),
        patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock(side_effect=RemoteWriteError("VM down"))),
        patch("app.metrics.writer.dedupe.release_log_ids", new=mock_release),
        patch("app.metrics.writer.snapshot_cache.upsert_snapshot", new=mock_upsert),
        patch("app.metrics.writer.advance_watermark", new=mock_advance),
    ):
        result = await writer._flush_batch(prepared)

    assert result is False
    mock_release.assert_called_once()
    mock_upsert.assert_not_called()
    mock_advance.assert_not_called()


@pytest.mark.asyncio
async def test_flush_failure_no_commit_no_cache() -> None:
    """remote_write 失败 → upsert_snapshot 和 advance_watermark 均不调用（C-METRIC-WRITE-1）。"""
    writer = _make_writer()
    msg = _make_msg()
    await writer.handle_message(msg)
    prepared = list(writer._pending)
    log_ids = {p.log_id for p in prepared}

    mock_upsert = AsyncMock()
    mock_advance = AsyncMock()

    with (
        patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=log_ids)),
        patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock(side_effect=RemoteWriteError("fail"))),
        patch("app.metrics.writer.dedupe.release_log_ids", new=AsyncMock()),
        patch("app.metrics.writer.snapshot_cache.upsert_snapshot", new=mock_upsert),
        patch("app.metrics.writer.advance_watermark", new=mock_advance),
    ):
        await writer._flush_batch(prepared)

    mock_upsert.assert_not_called()
    mock_advance.assert_not_called()


# ── 崩溃重投：全批已占用 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_log_ids_crash_recovery_returns_true() -> None:
    """全批 log_id 均为重复投递（崩溃重投）→ kept 为空 → 返回 True，不重写 VM。"""
    writer = _make_writer()
    msg = _make_msg()
    await writer.handle_message(msg)
    prepared = list(writer._pending)

    with (
        patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=set())),
        patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock()) as mock_rw,
    ):
        result = await writer._flush_batch(prepared)

    assert result is True
    mock_rw.assert_not_called()


# ── 指标计数 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_counters_increment_on_success() -> None:
    """成功 flush 后 accepted_total / samples_total 递增。"""
    import app.metrics.writer as writer_mod

    # 替换模块级 _metrics 单例
    local_metrics = MetricsMetrics()
    original = writer_mod._metrics  # type: ignore[attr-defined]
    writer_mod._metrics = local_metrics  # type: ignore[attr-defined]

    try:
        writer = _make_writer()
        writer._redis = AsyncMock()
        msg = _make_msg()
        await writer.handle_message(msg)
        prepared = list(writer._pending)
        log_ids = {p.log_id for p in prepared}

        with (
            patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=log_ids)),
            patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock()),
            patch("app.metrics.writer.snapshot_cache.upsert_snapshot", new=AsyncMock()),
            patch("app.metrics.writer.advance_watermark", new=AsyncMock()),
        ):
            await writer._flush_batch(prepared)

        snap = local_metrics.snapshot()
        assert snap.get("amp_metrics_writer_accepted_total", 0) >= 1
        assert snap.get("amp_metrics_writer_samples_total", 0) >= 1
    finally:
        writer_mod._metrics = original  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_remote_write_failure_counter_increments() -> None:
    """remote_write 失败时 remote_write_failures_total 递增。"""
    import app.metrics.writer as writer_mod

    local_metrics = MetricsMetrics()
    original = writer_mod._metrics  # type: ignore[attr-defined]
    writer_mod._metrics = local_metrics  # type: ignore[attr-defined]

    try:
        writer = _make_writer()
        msg = _make_msg()
        await writer.handle_message(msg)
        prepared = list(writer._pending)
        log_ids = {p.log_id for p in prepared}

        with (
            patch("app.metrics.writer.dedupe.claim_log_ids", new=AsyncMock(return_value=log_ids)),
            patch("app.metrics.writer.tsdb.remote_write", new=AsyncMock(side_effect=RemoteWriteError("fail"))),
            patch("app.metrics.writer.dedupe.release_log_ids", new=AsyncMock()),
        ):
            await writer._flush_batch(prepared)

        snap = local_metrics.snapshot()
        assert snap.get("amp_metrics_remote_write_failures_total", 0) >= 1
    finally:
        writer_mod._metrics = original  # type: ignore[attr-defined]
