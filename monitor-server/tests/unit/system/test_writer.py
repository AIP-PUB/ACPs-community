"""tests/unit/system/test_writer.py — writer.py 单元测试。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.system.exception import InvalidSystemRecordError


def _make_msg(
    log_type: str = "system",
    log_id: str = "log-001",
    body: dict | None = None,
    partition: int = 0,
    timestamp: str = "2024-06-14T12:00:00Z",
) -> MagicMock:
    msg = MagicMock()
    raw = {
        "schema_version": "1.0",
        "log_type": log_type,
        "log_id": log_id,
        "timestamp": timestamp,
        "aic": "aic-001",
        "body": body if body is not None else {"message": "hello"},
    }
    msg.value = json.dumps(raw).encode()
    msg.partition = partition
    return msg


def _make_redis() -> AsyncMock:
    store: dict[str, str] = {}
    sets: dict[str, set[str]] = {}

    async def get(key: str) -> str | None:
        return store.get(key)

    async def set_(key: str, val: str) -> None:
        store[key] = val

    async def sadd(key: str, *members: str) -> None:
        sets.setdefault(key, set()).update(members)

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=get)
    redis.set = AsyncMock(side_effect=set_)
    redis.sadd = AsyncMock(side_effect=sadd)
    return redis


def _make_writer(redis: Any = None) -> Any:
    from app.system.writer import SystemWriter

    if redis is None:
        redis = _make_redis()

    with patch("app.system.writer.settings") as mock_settings:
        mock_settings.system_topic = "amp.system"
        mock_settings.system_dlq_topic = "amp.system.dlq"
        mock_settings.system_consumer_group = "test-group"
        mock_settings.kafka_bootstrap_servers = "localhost:19092"
        mock_settings.kafka_security_protocol = "PLAINTEXT"
        mock_settings.kafka_auto_offset_reset = "earliest"
        mock_settings.kafka_max_poll_records = 100
        mock_settings.kafka_session_timeout_ms = 30000
        mock_settings.kafka_heartbeat_interval_ms = 10000
        mock_settings.system_search_text_max_length = 1000
        mock_settings.system_bulk_index_batch_interval_seconds = 5
        mock_settings.system_bulk_index_batch_max_docs = 5000
        mock_settings.system_freshness_reorder_margin_ms = 1000

        writer = SystemWriter(redis)
    return writer, redis, mock_settings


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_system_log_type_added_to_pending(self) -> None:
        writer, _redis, _mock_settings = _make_writer()
        msg = _make_msg(log_type="system")

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            await writer.handle_message(msg)

        assert len(writer._pending) == 1
        assert writer._pending[0].doc.log_id == "log-001"

    @pytest.mark.asyncio
    async def test_non_system_log_type_skipped(self) -> None:
        """非 system logType → skip（不加入 _pending）。"""
        writer, _, _ = _make_writer()
        msg = _make_msg(log_type="message")

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            await writer.handle_message(msg)

        assert len(writer._pending) == 0

    @pytest.mark.asyncio
    async def test_empty_value_skipped(self) -> None:
        writer, _, _ = _make_writer()
        msg = MagicMock()
        msg.value = None

        await writer.handle_message(msg)
        assert len(writer._pending) == 0

    @pytest.mark.asyncio
    async def test_invalid_timestamp_raises(self) -> None:
        writer, _, _ = _make_writer()
        msg = _make_msg(timestamp="not-a-timestamp")

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            with pytest.raises(InvalidSystemRecordError):
                await writer.handle_message(msg)


class TestProcessWithRetry:
    @pytest.mark.asyncio
    async def test_bad_json_sends_to_dlq_returns_false(self) -> None:
        """坏 JSON → DLQ，不重试，返回 False（坏数据不阻塞批）。"""
        writer, _, _ = _make_writer()
        msg = MagicMock()
        msg.value = b"not-valid-json"
        writer._send_to_dlq = AsyncMock()

        result = await writer._process_with_retry(msg)
        assert result is False
        writer._send_to_dlq.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_timestamp_sends_to_dlq(self) -> None:
        """无效 timestamp → DLQ，不重试。"""
        writer, _, _ = _make_writer()
        msg = _make_msg(timestamp="bad-ts")
        writer._send_to_dlq = AsyncMock()

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            result = await writer._process_with_retry(msg)
        assert result is False
        writer._send_to_dlq.assert_called_once()


class TestFlushBatch:
    @pytest.mark.asyncio
    async def test_bulk_transient_failure_returns_false(self) -> None:
        """transient bulk 失败 → return False（整批重试，不 commit，不推水位）。"""
        writer, redis, _ = _make_writer()
        msg = _make_msg()
        writer._pending = []

        # populate _pending manually
        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            await writer.handle_message(msg)

        from app.system.exception import OpenSearchBulkError

        with patch("app.system.store.bulk_index", side_effect=OpenSearchBulkError("transient")):
            with patch("app.system.writer.settings") as s:
                s.system_freshness_reorder_margin_ms = 1000
                result = await writer._flush_batch()

        assert result is False
        # watermark should not have been advanced
        redis.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_success_returns_true_and_advances_watermark(self) -> None:
        """bulk 成功 → return True 且推进水位。"""
        writer, redis, _ = _make_writer()
        msg = _make_msg()

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            await writer.handle_message(msg)

        from app.system.store import BulkResult

        with patch("app.system.store.bulk_index", return_value=BulkResult(indexed=1, failed_items=[])):
            with patch("app.system.writer.settings") as s:
                s.system_freshness_reorder_margin_ms = 1000
                result = await writer._flush_batch()

        assert result is True
        redis.set.assert_called()  # watermark was advanced

    @pytest.mark.asyncio
    async def test_permanent_failure_goes_to_dlq(self) -> None:
        """permanent 失败项投 DLQ，其余成功照写（bulk 整体 return True）。"""
        writer, _, _ = _make_writer()
        msg = _make_msg(log_id="log-fail")
        writer._send_to_dlq = AsyncMock()

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            await writer.handle_message(msg)

        from app.system.store import BulkResult

        with (
            patch(
                "app.system.store.bulk_index",
                return_value=BulkResult(indexed=0, failed_items=[("log-fail", "mapper conflict")]),
            ),
            patch("app.system.writer.settings") as s,
        ):
            s.system_freshness_reorder_margin_ms = 1000
            result = await writer._flush_batch()

        assert result is True
        writer._send_to_dlq.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_dedup_redis_calls_for_same_log_id(self) -> None:
        """C-SYSTEM-WRITE-6：同 log_id 重放无 Redis 去重调用（_id upsert 即幂等）。

        与 message/access writer 不同，不调用 dedupe.filter_unseen/mark_seen。
        """
        writer, redis, _ = _make_writer()

        # 两条相同 log_id
        msg1 = _make_msg(log_id="same-id")
        msg2 = _make_msg(log_id="same-id")

        with patch("app.system.writer.settings") as s:
            s.system_search_text_max_length = 1000
            await writer.handle_message(msg1)
            await writer.handle_message(msg2)

        assert len(writer._pending) == 2  # 都入了 pending（无批内去重约束）

        from app.system.store import BulkResult

        with (
            patch(
                "app.system.store.bulk_index",
                return_value=BulkResult(indexed=2, failed_items=[]),
            ) as mock_bulk,
            patch("app.system.writer.settings") as s,
        ):
            s.system_freshness_reorder_margin_ms = 1000
            await writer._flush_batch()

        # bulk_index 被调用（两条 upsert，OpenSearch 保证幂等，无 dedupe.filter_unseen）
        mock_bulk.assert_called_once()
        # Redis 没有专用去重键的 set/get（只有水位推进的 set）
        # 检查没有对 "dedup:" 前缀键的访问
        for call in redis.set.call_args_list:
            key = call.args[0] if call.args else call.kwargs.get("key", "")
            assert not key.startswith("dedup:"), f"Unexpected dedup Redis key: {key}"
