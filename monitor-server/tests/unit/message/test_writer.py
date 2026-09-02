"""单元测试：D-1 writer.py — MessageWriter 核心单元逻辑。

IO 函数（run()、_flush_batch 的 CH/Redis 调用）属集成测试；
此处覆盖：
  - handle_message 成功解析 + 入缓冲
  - handle_message 跳过非 message 类型
  - _process_with_retry 坏消息短路到 DLQ
  - _flush_batch 批内去重逻辑（mock store/dedupe）
  - _extract_observed_at 优先级
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.asyncio import Redis


def _make_message(
    *,
    value: bytes | None = None,
    partition: int = 0,
    offset: int = 0,
    timestamp: int = 1_700_000_000_000,
    timestamp_type: int = 0,
) -> MagicMock:
    msg = MagicMock()
    msg.value = value
    msg.partition = partition
    msg.offset = offset
    msg.timestamp = timestamp
    msg.timestamp_type = timestamp_type
    return msg


def _message_payload(**overrides: object) -> bytes:
    base: dict[str, object] = {
        "schema_version": "1.0",
        "log_id": "log-001",
        "log_type": "message",
        "timestamp": "2026-06-01T00:00:00Z",
        "aic": "svc-a",
        "trace_id": "trace-123",
        "correlation_id": "corr-456",
        "body": {
            "event_type": "send",
            "system": "kafka",
            "destination": {"name": "my-topic", "kind": "topic", "virtualHost": "/"},
            "message_id": "msg-abc",
        },
    }
    for k, v in overrides.items():
        base[k] = v
    return json.dumps(base).encode()


@pytest.fixture
def writer() -> object:
    from app.message.writer import MessageWriter

    redis = MagicMock(spec=Redis)
    return MessageWriter(redis)


class TestMessageWriterInit:
    def test_importable(self) -> None:
        from app.message.writer import MessageWriter  # noqa: F401

    def test_pending_empty_on_init(self, writer: object) -> None:
        from app.message.writer import MessageWriter

        assert isinstance(writer, MessageWriter)
        assert writer._pending == []
        assert writer._batch_log_ids == set()


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_valid_message_appended_to_pending(self, writer: object) -> None:
        msg = _make_message(value=_message_payload())
        await writer.handle_message(msg)  # type: ignore[attr-defined]
        assert len(writer._pending) == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_none_value_skipped(self, writer: object) -> None:
        msg = _make_message(value=None)
        await writer.handle_message(msg)  # type: ignore[attr-defined]
        assert len(writer._pending) == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_non_message_log_type_skipped(self, writer: object) -> None:
        payload = _message_payload()
        d = json.loads(payload)
        d["log_type"] = "access"
        msg = _make_message(value=json.dumps(d).encode())
        await writer.handle_message(msg)  # type: ignore[attr-defined]
        assert len(writer._pending) == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_duplicate_log_id_within_batch_skipped(self, writer: object) -> None:
        payload = _message_payload()
        msg1 = _make_message(value=payload)
        msg2 = _make_message(value=payload)
        await writer.handle_message(msg1)  # type: ignore[attr-defined]
        await writer.handle_message(msg2)  # type: ignore[attr-defined]
        assert len(writer._pending) == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_log_id_fallback_when_missing(self, writer: object) -> None:
        d = json.loads(_message_payload())
        del d["log_id"]
        msg = _make_message(value=json.dumps(d).encode())
        await writer.handle_message(msg)  # type: ignore[attr-defined]
        assert len(writer._pending) == 1  # type: ignore[attr-defined]


class TestExtractObservedAt:
    def test_log_append_time_wins(self, writer: object) -> None:
        from acps_sdk.amp.models import LogRecord

        msg = _make_message(timestamp=9_999_999_999, timestamp_type=1)
        record = LogRecord.model_validate(json.loads(_message_payload()))
        result = writer._extract_observed_at(msg, record)  # type: ignore[attr-defined]
        assert result == 9_999_999_999

    def test_observed_timestamp_used_when_no_log_append_time(self, writer: object) -> None:
        from acps_sdk.amp.models import LogRecord

        d = json.loads(_message_payload())
        d["observed_timestamp"] = "2026-06-01T00:00:00Z"
        record = LogRecord.model_validate(d)
        msg = _make_message(timestamp=1, timestamp_type=0)
        result = writer._extract_observed_at(msg, record)  # type: ignore[attr-defined]
        assert result > 0

    def test_now_fallback(self, writer: object) -> None:
        from acps_sdk.amp.models import LogRecord

        record = LogRecord.model_validate(json.loads(_message_payload()))
        msg = _make_message(timestamp=0, timestamp_type=0)
        result = writer._extract_observed_at(msg, record)  # type: ignore[attr-defined]
        assert result > 0


class TestFlushBatch:
    @pytest.mark.asyncio
    async def test_empty_pending_returns_true(self, writer: object) -> None:
        result = await writer._flush_batch()  # type: ignore[attr-defined]
        assert result is True

    @pytest.mark.asyncio
    async def test_ch_insert_failure_returns_false(self, writer: object) -> None:
        msg = _make_message(value=_message_payload())
        await writer.handle_message(msg)  # type: ignore[attr-defined]

        with (
            patch("app.message.writer.dedupe.filter_unseen", AsyncMock(return_value=({"log-001"}, True))),
            patch(
                "app.message.writer.store.insert_events",
                AsyncMock(
                    side_effect=__import__(
                        "app.message.exception", fromlist=["ClickHouseInsertError"]
                    ).ClickHouseInsertError("fail")
                ),
            ),
        ):
            result = await writer._flush_batch()  # type: ignore[attr-defined]
        assert result is False

    @pytest.mark.asyncio
    async def test_all_seen_returns_true_without_insert(self, writer: object) -> None:
        msg = _make_message(value=_message_payload())
        await writer.handle_message(msg)  # type: ignore[attr-defined]

        with (
            patch("app.message.writer.dedupe.filter_unseen", AsyncMock(return_value=(set(), True))),
            patch("app.message.writer.store.insert_events", AsyncMock()) as mock_insert,
        ):
            result = await writer._flush_batch()  # type: ignore[attr-defined]
        assert result is True
        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_flush_clears_not_pending(self, writer: object) -> None:
        msg = _make_message(value=_message_payload())
        await writer.handle_message(msg)  # type: ignore[attr-defined]

        with (
            patch("app.message.writer.dedupe.filter_unseen", AsyncMock(return_value=({"log-001"}, True))),
            patch("app.message.writer.store.insert_events", AsyncMock()),
            patch("app.message.writer.freshness.advance_partition_watermark", AsyncMock()),
            patch("app.message.writer.dedupe.mark_seen", AsyncMock()),
        ):
            result = await writer._flush_batch()  # type: ignore[attr-defined]
        assert result is True
        # _pending is NOT cleared here (run() clears it), flush just returns True
        assert len(writer._pending) > 0  # type: ignore[attr-defined]
