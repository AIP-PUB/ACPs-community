"""HeartbeatWriter 单元测试（Step 5 TDD 红阶段）。

覆盖：
- observed_at_ms 提取优先级（LogAppendTime → observedTimestamp → DLQ）
- source_timestamp_ms 提取
- apply_heartbeat 调用参数正确性
- 水位按分区推进（partition_watermark_ms）
- 水位刷新到 Redis（write_watermarks）
- DLQ 路径（缺失时间戳）
- 指标计数（accepted / ignored_older / delta counts）
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 辅助函数：构造 aiokafka ConsumerRecord Mock ─────────────────────────────


def _make_msg(
    *,
    value: bytes,
    partition: int = 0,
    offset: int = 100,
    timestamp: int | None = None,
    timestamp_type: int = 1,  # 1 = LogAppendTime
    key: bytes | None = None,
) -> MagicMock:
    """构造模拟 aiokafka ConsumerRecord。

    Args:
        value: 消息 value 字节。
        partition: Kafka 分区号。
        offset: 消息偏移量。
        timestamp: Kafka 消息时间戳（epoch ms）。
        timestamp_type: 0=CREATE_TIME, 1=LOG_APPEND_TIME。
        key: 消息 key。
    """
    msg = MagicMock()
    msg.value = value
    msg.partition = partition
    msg.offset = offset
    msg.timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
    msg.timestamp_type = timestamp_type
    msg.key = key
    msg.topic = "amp.heartbeat"
    return msg


def _make_hb_value(
    aic: str = "test-aic-001",
    observed_timestamp: str | None = None,
    source_timestamp: str | None = None,
) -> bytes:
    """构造 Heartbeat LogRecord JSON 字节。

    Args:
        aic: Agent Identity Code。
        observed_timestamp: observedTimestamp ISO 字符串（可选）。
        source_timestamp: timestamp ISO 字符串（可选）。
    """
    import json

    record: dict[str, Any] = {
        "aic": aic,
        "logType": "heartbeat",
        "logId": "test-log-id-001",
    }
    if observed_timestamp:
        record["observedTimestamp"] = observed_timestamp
    if source_timestamp:
        record["timestamp"] = source_timestamp
    return json.dumps(record).encode("utf-8")


# ── 基础 Mocks ──────────────────────────────────────────────────────────────

BASE_MS = 1_700_000_000_000  # 固定基准 epoch ms
BASE_ISO = "2023-11-14T22:13:20+00:00"  # 对应的 ISO 字符串
SOURCE_MS = 1_700_000_001_000
SOURCE_ISO = "2023-11-14T22:13:21+00:00"


def _make_apply_result(
    status: str = "applied_with_delta",
    kind: str | None = "enter_alive",
    seq: int | None = 1,
) -> MagicMock:
    """构造模拟 ApplyResult。"""
    result = MagicMock()
    result.status = status
    result.kind = kind
    result.seq = seq
    return result


# ── Tests ──────────────────────────────────────────────────────────────────


class TestObservedAtExtraction:
    """observed_at_ms 提取逻辑（§3.1 优先级：LogAppendTime → observedTimestamp → DLQ）。"""

    @pytest.mark.asyncio
    async def test_uses_kafka_log_append_time_when_available(self) -> None:
        """LogAppendTime（timestamp_type=1）应作为 observed_at_ms。"""
        from app.heartbeat.writer import HeartbeatWriter

        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        msg = _make_msg(
            value=_make_hb_value(),
            timestamp=BASE_MS,
            timestamp_type=1,
        )
        observed, _source = writer._extract_timestamps(msg, {})
        assert observed == BASE_MS
        """timestamp_type=0（CREATE_TIME）时，回退到 observedTimestamp 字段。"""
        from app.heartbeat.writer import HeartbeatWriter

        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        observed_iso = BASE_ISO
        raw = {"observedTimestamp": observed_iso}
        msg = _make_msg(
            value=_make_hb_value(observed_timestamp=observed_iso),
            timestamp=12345,
            timestamp_type=0,
        )
        observed, _source = writer._extract_timestamps(msg, raw)
        assert observed == BASE_MS

    @pytest.mark.asyncio
    async def test_returns_none_when_no_usable_timestamp(self) -> None:
        """无 LogAppendTime 且无 observedTimestamp 时，返回 None（触发 DLQ）。"""
        from app.heartbeat.writer import HeartbeatWriter

        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        msg = _make_msg(
            value=_make_hb_value(),
            timestamp=0,
            timestamp_type=0,  # CREATE_TIME but missing observedTimestamp
        )
        observed, _source = writer._extract_timestamps(msg, {})
        assert observed is None

    @pytest.mark.asyncio
    async def test_extracts_source_timestamp(self) -> None:
        """有 timestamp 字段时，source_timestamp_ms 应被提取。"""
        from app.heartbeat.writer import HeartbeatWriter

        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        raw = {"timestamp": SOURCE_ISO}
        msg = _make_msg(
            value=_make_hb_value(source_timestamp=SOURCE_ISO),
            timestamp=BASE_MS,
            timestamp_type=1,
        )
        observed, source = writer._extract_timestamps(msg, raw)
        assert observed == BASE_MS
        assert source == SOURCE_MS

    @pytest.mark.asyncio
    async def test_source_timestamp_is_none_when_missing(self) -> None:
        """无 timestamp 字段时，source_timestamp_ms 为 None。"""
        from app.heartbeat.writer import HeartbeatWriter

        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        msg = _make_msg(value=_make_hb_value(), timestamp=BASE_MS, timestamp_type=1)
        _observed, source = writer._extract_timestamps(msg, {})
        assert source is None


class TestHandleMessage:
    """handle_message 调用 apply_heartbeat 并更新水位。"""

    @pytest.mark.asyncio
    async def test_calls_apply_heartbeat_with_correct_args(self) -> None:
        """handle_message 应将正确的 aic / observed_at_ms 传给 apply_heartbeat。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied_with_delta", "enter_alive", 1))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0

        msg = _make_msg(value=_make_hb_value(aic="test-aic-001"), timestamp=BASE_MS, timestamp_type=1)

        redis_mock = AsyncMock()
        writer._redis = redis_mock

        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        apply_mock.assert_awaited_once()
        call_kwargs = apply_mock.call_args.kwargs
        assert call_kwargs["aic"] == "test-aic-001"
        assert call_kwargs["observed_at_ms"] == BASE_MS

    @pytest.mark.asyncio
    async def test_increments_accepted_on_apply(self) -> None:
        """正常处理后 _accepted 计数应加 1。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied", None, None))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), timestamp=BASE_MS, timestamp_type=1)
        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        assert writer._accepted == 1

    @pytest.mark.asyncio
    async def test_increments_ignored_older_on_stale(self) -> None:
        """status=ignored_older 时 _ignored_older 计数应加 1，_accepted 不增。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("ignored_older", None, None))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), timestamp=BASE_MS, timestamp_type=1)
        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        assert writer._ignored_older == 1
        assert writer._accepted == 0

    @pytest.mark.asyncio
    async def test_increments_enter_alive_on_delta(self) -> None:
        """status=applied_with_delta kind=enter_alive 时 _enter_alive 计数增加。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied_with_delta", "enter_alive", 1))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), timestamp=BASE_MS, timestamp_type=1)
        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        assert writer._enter_alive == 1

    @pytest.mark.asyncio
    async def test_increments_refresh_alive_on_delta(self) -> None:
        """status=applied_with_delta kind=refresh_alive 时 _refresh_alive 计数增加。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied_with_delta", "refresh_alive", 2))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), timestamp=BASE_MS, timestamp_type=1)
        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        assert writer._refresh_alive == 1

    @pytest.mark.asyncio
    async def test_raises_on_missing_timestamp(self) -> None:
        """无法提取 observed_at_ms 时，handle_message 应抛出 UntimedHeartbeatError。

        A-3 修复说明：该异常在 _process_with_retry() 中被拦截，直接写 DLQ（跳过重试）；
        handle_message 本身继续抛出该异常以便调用方按策略路由。
        """
        from app.heartbeat.writer import HeartbeatWriter

        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        from app.heartbeat.exception import UntimedHeartbeatError

        msg = _make_msg(
            value=_make_hb_value(),
            timestamp=0,
            timestamp_type=0,  # CREATE_TIME, 无 observedTimestamp
        )
        with pytest.raises(UntimedHeartbeatError):
            await writer.handle_message(msg)


class TestWatermarkTracking:
    """水位追踪（按分区 partition_watermark_ms 推进）。"""

    @pytest.mark.asyncio
    async def test_partition_watermark_updated_after_apply(self) -> None:
        """apply_heartbeat 成功后，对应分区水位应更新为 observed_at_ms。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied", None, None))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), partition=2, timestamp=BASE_MS, timestamp_type=1)
        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        assert writer._partition_watermarks.get(2) == BASE_MS

    @pytest.mark.asyncio
    async def test_partition_watermark_monotonically_increases(self) -> None:
        """同一分区水位只能向前推进，不回退。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied", None, None))
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {0: BASE_MS + 5000}  # 已有较新水位
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        # 写入较旧的 observed_at_ms
        msg = _make_msg(value=_make_hb_value(), partition=0, timestamp=BASE_MS, timestamp_type=1)
        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        assert writer._partition_watermarks[0] == BASE_MS + 5000  # 不回退

    @pytest.mark.asyncio
    async def test_watermark_flushed_to_redis_when_interval_elapsed(self) -> None:
        """超过 flush 间隔时，_flush_watermarks 应将水位写入 Redis。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied", None, None))
        flush_mock = AsyncMock()
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0  # 从未 flush，必然超过间隔
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), partition=0, timestamp=BASE_MS, timestamp_type=1)
        with (
            patch("app.heartbeat.writer.apply_heartbeat", apply_mock),
            patch.object(writer, "_flush_watermarks", flush_mock),
        ):
            await writer.handle_message(msg)

        flush_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_watermark_not_flushed_when_interval_not_elapsed(self) -> None:
        """未超过 flush 间隔时，_flush_watermarks 不被调用。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock(return_value=_make_apply_result("applied", None, None))
        flush_mock = AsyncMock()
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = time.monotonic()  # 刚刚 flush
        writer._redis = AsyncMock()

        msg = _make_msg(value=_make_hb_value(), partition=0, timestamp=BASE_MS, timestamp_type=1)
        with (
            patch("app.heartbeat.writer.apply_heartbeat", apply_mock),
            patch.object(writer, "_flush_watermarks", flush_mock),
        ):
            await writer.handle_message(msg)

        flush_mock.assert_not_awaited()


class TestFlushWatermarks:
    """_flush_watermarks 向 Redis 写入水位数据。"""

    @pytest.mark.asyncio
    async def test_flush_calls_write_watermarks(self) -> None:
        """_flush_watermarks 应调用 store.write_watermarks 并更新 _last_watermark_flush_at。

        A-2 修复：updated_at_ms 使用 Redis TIME（redis_now_ms），需同步 mock。
        """
        from app.heartbeat.writer import HeartbeatWriter

        write_mock = AsyncMock()
        redis_now_ms_mock = AsyncMock(return_value=BASE_MS)
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._partition_watermarks = {0: BASE_MS, 1: BASE_MS + 1000}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        with (
            patch("app.heartbeat.writer.write_watermarks", write_mock),
            patch("app.heartbeat.writer.redis_now_ms", redis_now_ms_mock),
        ):
            await writer._flush_watermarks()

        write_mock.assert_awaited_once()
        assert writer._last_watermark_flush_at > 0

    @pytest.mark.asyncio
    async def test_flush_skips_when_no_watermarks(self) -> None:
        """空水位时，_flush_watermarks 不调用 store.write_watermarks。"""
        from app.heartbeat.writer import HeartbeatWriter

        write_mock = AsyncMock()
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        with patch("app.heartbeat.writer.write_watermarks", write_mock):
            await writer._flush_watermarks()

        write_mock.assert_not_awaited()


class TestHeartbeatWriterInit:
    """HeartbeatWriter 初始化与 BaseLogConsumer 集成。"""

    def test_init_uses_heartbeat_topic(self) -> None:
        """HeartbeatWriter 初始化时应使用 settings.heartbeat_topic。"""
        from app.core.config import settings
        from app.heartbeat.writer import HeartbeatWriter

        # 构造真实实例（需要 Redis）
        redis_mock = AsyncMock()
        writer = HeartbeatWriter(redis=redis_mock)
        assert writer._topic == settings.heartbeat_topic
        assert writer._dlq_topic == settings.heartbeat_dlq_topic

    def test_init_counters_zeroed(self) -> None:
        """初始化后所有计数器应为 0。"""
        from app.heartbeat.writer import HeartbeatWriter

        redis_mock = AsyncMock()
        writer = HeartbeatWriter(redis=redis_mock)
        assert writer._accepted == 0
        assert writer._ignored_older == 0
        assert writer._enter_alive == 0
        assert writer._refresh_alive == 0


class TestNonHeartbeatMessages:
    """非 heartbeat logType 消息应被静默跳过。"""

    @pytest.mark.asyncio
    async def test_skips_non_heartbeat_log_type(self) -> None:
        """logType != heartbeat 的消息应被跳过，apply_heartbeat 不被调用。"""
        import json

        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock()
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        audit_value = json.dumps({"aic": "x", "logType": "audit"}).encode("utf-8")
        msg = _make_msg(value=audit_value, timestamp=BASE_MS, timestamp_type=1)

        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        apply_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_empty_message(self) -> None:
        """空 value 消息应被静默跳过，apply_heartbeat 不被调用。"""
        from app.heartbeat.writer import HeartbeatWriter

        apply_mock = AsyncMock()
        writer = HeartbeatWriter.__new__(HeartbeatWriter)
        writer._accepted = 0
        writer._ignored_older = 0
        writer._enter_alive = 0
        writer._refresh_alive = 0
        writer._partition_watermarks = {}
        writer._last_watermark_flush_at = 0.0
        writer._redis = AsyncMock()

        msg = _make_msg(value=b"", timestamp=BASE_MS, timestamp_type=1)

        with patch("app.heartbeat.writer.apply_heartbeat", apply_mock):
            await writer.handle_message(msg)

        apply_mock.assert_not_awaited()
