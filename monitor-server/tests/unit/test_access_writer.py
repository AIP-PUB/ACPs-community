"""tests/unit/test_access_writer.py — AccessWriter 消费逻辑测试。

TDD D-1：先写测试（红）→ 实现 writer.py（绿）。
全部 Mock Kafka/Redis/CH；不做实际 I/O。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_writer(redis: Any = None) -> Any:
    from app.access.writer import AccessWriter

    r = redis or AsyncMock()
    return AccessWriter(r)


def _make_kafka_message(value: bytes | None, partition: int = 0, timestamp: int = 1_700_000_000_000) -> Any:
    msg = MagicMock()
    msg.value = value
    msg.partition = partition
    msg.timestamp = timestamp
    msg.timestamp_type = 1  # LogAppendTime
    msg.topic = "amp.access"
    msg.offset = 0
    return msg


def _make_access_record_dict(log_id: str = "lid-1") -> dict:
    return {
        "schema_version": "1.0",
        "timestamp": "2026-01-01T00:00:00Z",
        "aic": "aic-test",
        "log_type": "access",
        "log_id": log_id,
        "body": {
            "durationMs": 100.0,
        },
    }


class TestAccessWriterInit:
    def test_can_instantiate(self) -> None:
        w = _make_writer()
        assert w is not None

    def test_has_pending_list(self) -> None:
        w = _make_writer()
        assert hasattr(w, "_pending")
        assert isinstance(w._pending, list)

    def test_has_batch_log_ids(self) -> None:
        w = _make_writer()
        assert hasattr(w, "_batch_log_ids")
        assert isinstance(w._batch_log_ids, set)


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_valid_access_record_added_to_pending(self) -> None:
        w = _make_writer()
        msg_dict = _make_access_record_dict("lid-1")
        import json

        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        await w.handle_message(msg)
        assert len(w._pending) == 1
        assert w._pending[0].log_id == "lid-1"

    @pytest.mark.asyncio
    async def test_non_access_log_type_skipped(self) -> None:
        w = _make_writer()
        msg_dict = {
            "schema_version": "1.0",
            "timestamp": "2026-01-01T00:00:00Z",
            "aic": "aic-test",
            "log_type": "heartbeat",
            "body": {},
        }
        import json

        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        await w.handle_message(msg)
        assert len(w._pending) == 0

    @pytest.mark.asyncio
    async def test_none_value_skipped(self) -> None:
        w = _make_writer()
        msg = _make_kafka_message(None)
        await w.handle_message(msg)
        assert len(w._pending) == 0

    @pytest.mark.asyncio
    async def test_duplicate_log_id_in_batch_deduplicated(self) -> None:
        """批内内存去重：同 log_id 第二次不入 pending。"""
        w = _make_writer()
        import json

        msg_dict = _make_access_record_dict("dup-id")
        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        await w.handle_message(msg)
        await w.handle_message(msg)
        assert len(w._pending) == 1
        assert len(w._batch_log_ids) == 1

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self) -> None:
        """无效 JSON → 异常（_process_with_retry 负责 DLQ 路由，handle_message 不吞掉）。"""
        import json

        w = _make_writer()
        msg = _make_kafka_message(b"not-json")
        with pytest.raises(json.JSONDecodeError):
            await w.handle_message(msg)

    @pytest.mark.asyncio
    async def test_invalid_timestamp_raises_invalid_access_record_error(self) -> None:
        from app.access.exception import InvalidAccessRecordError

        w = _make_writer()
        import json

        msg_dict = _make_access_record_dict()
        msg_dict["timestamp"] = "not-a-date"
        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        with pytest.raises(InvalidAccessRecordError):
            await w.handle_message(msg)

    @pytest.mark.asyncio
    async def test_log_id_fallback_when_missing(self) -> None:
        """record.log_id 缺失时使用 compute_log_id_fallback（§5.1.3）。"""
        w = _make_writer()
        import json

        msg_dict = _make_access_record_dict()
        del msg_dict["log_id"]  # no log_id
        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        await w.handle_message(msg)
        assert len(w._pending) == 1
        # fallback log_id is non-empty
        assert w._pending[0].log_id


class TestProcessWithRetry:
    @pytest.mark.asyncio
    async def test_invalid_record_returns_false(self) -> None:
        """InvalidAccessRecordError → _process_with_retry 返回 False（不重试，直达 DLQ）。"""
        w = _make_writer()
        import json

        msg_dict = _make_access_record_dict()
        msg_dict["timestamp"] = "invalid"
        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        result = await w._process_with_retry(msg)
        assert result is False

    @pytest.mark.asyncio
    async def test_json_error_returns_false(self) -> None:
        w = _make_writer()
        msg = _make_kafka_message(b"bad-json")
        result = await w._process_with_retry(msg)
        assert result is False


class TestFlushBatch:
    @pytest.mark.asyncio
    async def test_empty_pending_returns_true(self) -> None:
        w = _make_writer()
        result = await w._flush_batch()
        assert result is True

    @pytest.mark.asyncio
    async def test_all_duplicates_returns_true_no_ch_write(self) -> None:
        """全是已去重的消息 → flush 返回 True 且不写 CH。"""
        import json

        w = _make_writer()
        msg_dict = _make_access_record_dict("lid-dup")
        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        await w.handle_message(msg)

        # Mark as seen (dedupe will say all are seen)
        with patch("app.access.writer.dedupe") as mock_dedupe:
            mock_dedupe.filter_unseen = AsyncMock(return_value=(set(), True))
            with patch("app.access.writer.store") as mock_store:
                mock_store.insert_events = AsyncMock()
                result = await w._flush_batch()
                assert result is True
                mock_store.insert_events.assert_not_called()

    @pytest.mark.asyncio
    async def test_ch_insert_failure_returns_false(self) -> None:
        import json

        from app.access.exception import ClickHouseInsertError

        w = _make_writer()
        msg_dict = _make_access_record_dict("lid-1")
        msg = _make_kafka_message(json.dumps(msg_dict).encode())
        await w.handle_message(msg)

        with patch("app.access.writer.dedupe") as mock_dedupe:
            mock_dedupe.filter_unseen = AsyncMock(return_value=({"lid-1"}, True))
            with patch("app.access.writer.store") as mock_store:
                mock_store.insert_events = AsyncMock(side_effect=ClickHouseInsertError("CH down"))
                result = await w._flush_batch()
                assert result is False

    @pytest.mark.asyncio
    async def test_successful_flush_marks_seen_and_advances_wm(self) -> None:
        import json

        w = _make_writer()
        msg_dict = _make_access_record_dict("lid-ok")
        msg = _make_kafka_message(json.dumps(msg_dict).encode(), partition=0)
        await w.handle_message(msg)

        with patch("app.access.writer.dedupe") as mock_dedupe:
            mock_dedupe.filter_unseen = AsyncMock(return_value=({"lid-ok"}, True))
            mock_dedupe.mark_seen = AsyncMock()
            with patch("app.access.writer.store") as mock_store:
                mock_store.insert_events = AsyncMock()
                with patch("app.access.writer.freshness") as mock_freshness:
                    mock_freshness.advance_partition_watermark = AsyncMock()
                    result = await w._flush_batch()
                    assert result is True
                    mock_dedupe.mark_seen.assert_called_once()
                    mock_freshness.advance_partition_watermark.assert_called()
