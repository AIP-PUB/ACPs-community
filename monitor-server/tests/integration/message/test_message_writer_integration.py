"""tests/integration/message/test_message_writer_integration.py — MessageWriter 集成测试（D-1）。

绕过 Kafka 消费循环，直接调用 writer.handle_message / _flush_batch 验证：
- CH 三步提交（insert → 推水位 → 写去重标记）
- 去重幂等性（同 log_id 第二次不写入 CH）
- CH 失败时 _flush_batch 返回 False

需要真实 ClickHouse + Redis（dev-infra）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.asyncio import Redis

from app.message.exception import ClickHouseInsertError
from tests.support.clickhouse_helper import make_message_event_row
from tests.support.constants import TEST_CLICKHOUSE_DATABASE, TEST_REDIS_URL
from tests.support.factory import make_message_log_record
from tests.support.redis_helper import reset_message_redis_state

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_message() -> AsyncGenerator[Redis]:
    r = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except Exception as exc:
        await r.aclose()
        pytest.skip(f"Redis 不可达，跳过集成测试：{exc}")
    await reset_message_redis_state(r)
    yield r
    await reset_message_redis_state(r)
    await r.aclose()


@pytest.fixture(autouse=True)
async def _deps(_require_clickhouse: None, isolated_message_clickhouse: None) -> None:
    """所有 writer 集成测试需要 CH schema + Message 表清空。"""


def _make_mock_message(
    *,
    log_id: str | None = None,
    event_type: str = "send",
    partition: int = 0,
) -> MagicMock:
    payload = make_message_log_record(log_id=log_id or str(uuid.uuid4()), event_type=event_type)
    msg = MagicMock()
    msg.value = json.dumps(payload).encode()
    msg.partition = partition
    msg.timestamp_type = 1  # LogAppendTime
    msg.timestamp = int(datetime.now(UTC).timestamp() * 1000)
    msg.offset = 0
    return msg


class TestFlushBatch:
    async def test_flush_batch_inserts_to_ch(self, redis_message: Redis) -> None:
        from app.core.clickhouse_client import get_clickhouse_client
        from app.message.tables import MESSAGE_EVENTS
        from app.message.writer import MessageWriter, _PreparedRecord

        writer = MessageWriter(redis_message)
        log_id = str(uuid.uuid4())
        row = make_message_event_row(log_id=log_id)
        writer._pending = [
            _PreparedRecord(
                log_id=log_id,
                partition=0,
                timestamp_ms=row.timestamp_ms,
                row=row,
            )
        ]

        result = await writer._flush_batch()
        assert result is True

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        query_result = await client.query(
            f"SELECT count() FROM `{db}`.`{MESSAGE_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        assert query_result.result_rows[0][0] == 1

    async def test_flush_batch_dedupes_second_write(self, redis_message: Redis) -> None:
        from app.core.clickhouse_client import get_clickhouse_client
        from app.message.tables import MESSAGE_EVENTS
        from app.message.writer import MessageWriter, _PreparedRecord

        writer = MessageWriter(redis_message)
        log_id = str(uuid.uuid4())
        row = make_message_event_row(log_id=log_id)
        pending = _PreparedRecord(
            log_id=log_id,
            partition=0,
            timestamp_ms=row.timestamp_ms,
            row=row,
        )

        writer._pending = [pending]
        await writer._flush_batch()
        writer._pending = [pending]
        await writer._flush_batch()

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{MESSAGE_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        assert result.result_rows[0][0] == 1

    async def test_flush_batch_returns_false_on_ch_error(self, redis_message: Redis) -> None:
        from app.message.writer import MessageWriter, _PreparedRecord

        writer = MessageWriter(redis_message)
        log_id = str(uuid.uuid4())
        row = make_message_event_row(log_id=log_id)
        writer._pending = [
            _PreparedRecord(
                log_id=log_id,
                partition=0,
                timestamp_ms=row.timestamp_ms,
                row=row,
            )
        ]

        with patch(
            "app.message.writer.store.insert_events",
            AsyncMock(side_effect=ClickHouseInsertError("CH down")),
        ):
            result = await writer._flush_batch()

        assert result is False


class TestHandleMessage:
    async def test_handle_valid_message_enqueues(self, redis_message: Redis) -> None:
        from app.message.writer import MessageWriter

        writer = MessageWriter(redis_message)
        await writer.handle_message(_make_mock_message())
        assert len(writer._pending) == 1

    async def test_handle_non_message_log_type_skipped(self, redis_message: Redis) -> None:
        from app.message.writer import MessageWriter

        writer = MessageWriter(redis_message)
        msg = MagicMock()
        msg.value = json.dumps({"log_type": "access", "aic": "x", "body": {}}).encode()
        msg.partition = 0
        msg.timestamp_type = 1
        msg.timestamp = 1000

        await writer.handle_message(msg)
        assert len(writer._pending) == 0

    async def test_batch_internal_dedup(self, redis_message: Redis) -> None:
        from app.message.writer import MessageWriter

        writer = MessageWriter(redis_message)
        log_id = str(uuid.uuid4())
        await writer.handle_message(_make_mock_message(log_id=log_id))
        await writer.handle_message(_make_mock_message(log_id=log_id))
        assert len(writer._pending) == 1


class TestEndToEndWithoutKafka:
    async def test_full_pipeline_message_to_ch(self, redis_message: Redis) -> None:
        from app.core.clickhouse_client import get_clickhouse_client
        from app.message.freshness import read_events_watermark
        from app.message.tables import MESSAGE_EVENTS
        from app.message.writer import MessageWriter

        writer = MessageWriter(redis_message)
        log_id = str(uuid.uuid4())
        await writer.handle_message(_make_mock_message(log_id=log_id))
        assert len(writer._pending) == 1

        result = await writer._flush_batch()
        assert result is True

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        query_result = await client.query(
            f"SELECT count() FROM `{db}`.`{MESSAGE_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        assert query_result.result_rows[0][0] == 1

        wm = await read_events_watermark(redis_message)
        assert wm is not None
