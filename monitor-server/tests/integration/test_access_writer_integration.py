"""tests/integration/test_access_writer_integration.py — AccessWriter 集成测试（D-1）。

绕过 Kafka 消费循环，直接调用 writer._flush_batch / handle_message 验证：
- CH 三步提交（insert → 推水位 → 写去重标记）
- 去重幂等性（同 log_id 第二次不写入 CH）
- CH 失败时 _flush_batch 返回 False

需要真实 ClickHouse + Redis（dev-infra）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.asyncio import Redis

from app.access.exception import ClickHouseInsertError
from tests.support.clickhouse_helper import make_access_event_row
from tests.support.constants import TEST_CLICKHOUSE_DATABASE, TEST_REDIS_URL
from tests.support.redis_helper import reset_access_redis_state


@pytest.fixture
async def redis_access() -> AsyncGenerator[Redis]:
    r = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await reset_access_redis_state(r)
    yield r
    await reset_access_redis_state(r)
    await r.aclose()


@pytest.fixture(autouse=True)
async def _deps(_require_clickhouse: None, isolated_clickhouse: None) -> None:
    """所有 writer 集成测试需要 CH schema + 表清空。"""


def _make_mock_message(
    *,
    log_id: str | None = None,
    aic: str = "aic-writer-001",
    response_status: int = 200,
    partition: int = 0,
) -> MagicMock:
    """构造模拟 Kafka 消息对象。"""
    import json

    from tests.support.factory import make_access_log_record

    payload = make_access_log_record(aic=aic, response_status=response_status)
    if log_id:
        payload["log_id"] = log_id

    msg = MagicMock()
    msg.value = json.dumps(payload).encode()
    msg.partition = partition
    msg.timestamp_type = 1  # LogAppendTime
    msg.timestamp = int(datetime.now(UTC).timestamp() * 1000)
    msg.offset = 0
    return msg


class TestFlushBatch:
    async def test_flush_batch_inserts_to_ch(self, redis_access: Redis) -> None:
        """_flush_batch 成功后 CH 中应有对应行。"""
        from app.access.tables import ACCESS_EVENTS
        from app.access.writer import AccessWriter
        from app.core.clickhouse_client import get_clickhouse_client

        writer = AccessWriter(redis_access)
        log_id = str(uuid.uuid4())
        row = make_access_event_row(log_id=log_id)

        from app.access.writer import _PreparedRecord

        writer._pending = [
            _PreparedRecord(
                log_id=log_id,
                partition=0,
                timestamp_ms=row.timestamp_ms,
                trace_id=row.trace_id,
                row=row,
                redacted_headers=0,
            )
        ]

        result = await writer._flush_batch()
        assert result is True

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        query_result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        count = query_result.result_rows[0][0]
        assert count == 1, f"CH 中应有 1 行，实际 {count}"

    async def test_flush_batch_advances_watermark(self, redis_access: Redis) -> None:
        """_flush_batch 后 Redis 中水位有更新。"""
        from app.access.freshness import read_overall_watermark
        from app.access.writer import AccessWriter, _PreparedRecord

        writer = AccessWriter(redis_access)
        ts_ms = int(datetime.now(UTC).timestamp() * 1000)
        row = make_access_event_row(log_id=str(uuid.uuid4()))
        row_ts = ts_ms
        writer._pending = [
            _PreparedRecord(
                log_id=row.log_id,
                partition=0,
                timestamp_ms=row_ts,
                trace_id=row.trace_id,
                row=row,
                redacted_headers=0,
            )
        ]

        await writer._flush_batch()

        wm = await read_overall_watermark(redis_access)
        assert wm is not None, "水位应已更新"

    async def test_flush_batch_marks_seen(self, redis_access: Redis) -> None:
        """_flush_batch 后 log_id 去重标记已写入 Redis。"""
        from app.access import dedupe
        from app.access.writer import AccessWriter, _PreparedRecord

        writer = AccessWriter(redis_access)
        log_id = str(uuid.uuid4())
        row = make_access_event_row(log_id=log_id)
        writer._pending = [
            _PreparedRecord(
                log_id=log_id,
                partition=0,
                timestamp_ms=row.timestamp_ms,
                trace_id=row.trace_id,
                row=row,
                redacted_headers=0,
            )
        ]

        await writer._flush_batch()

        seen_set, _ = await dedupe.filter_unseen(redis_access, [log_id])
        # 已被标记后，filter_unseen 应将其排除（seen_set 为空）
        assert log_id not in seen_set, f"{log_id} 应已被标记为 seen"

    async def test_flush_batch_idempotent_second_call(self, redis_access: Redis) -> None:
        """同 log_id 第二次 flush 不向 CH 写入重复行。"""
        from app.access.tables import ACCESS_EVENTS
        from app.access.writer import AccessWriter, _PreparedRecord
        from app.core.clickhouse_client import get_clickhouse_client

        writer = AccessWriter(redis_access)
        log_id = str(uuid.uuid4())
        row = make_access_event_row(log_id=log_id)

        pending = _PreparedRecord(
            log_id=log_id,
            partition=0,
            timestamp_ms=row.timestamp_ms,
            trace_id=row.trace_id,
            row=row,
            redacted_headers=0,
        )

        # 第一次 flush
        writer._pending = [pending]
        await writer._flush_batch()

        # 第二次 flush（模拟重试，log_id 已在去重标记里）
        writer._pending = [pending]
        await writer._flush_batch()

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_EVENTS}` FINAL")
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        count = result.result_rows[0][0]
        assert count == 1, f"幂等：应只有 1 行，实际 {count}"

    async def test_flush_batch_returns_false_on_ch_error(self, redis_access: Redis) -> None:
        """CH insert 失败时 _flush_batch 返回 False（fail-closed）。"""
        from app.access.writer import AccessWriter, _PreparedRecord

        writer = AccessWriter(redis_access)
        log_id = str(uuid.uuid4())
        row = make_access_event_row(log_id=log_id)
        writer._pending = [
            _PreparedRecord(
                log_id=log_id,
                partition=0,
                timestamp_ms=row.timestamp_ms,
                trace_id=row.trace_id,
                row=row,
                redacted_headers=0,
            )
        ]

        with patch("app.access.writer.store.insert_events", AsyncMock(side_effect=ClickHouseInsertError("CH down"))):
            result = await writer._flush_batch()

        assert result is False


class TestHandleMessage:
    async def test_handle_valid_message_enqueues(self, redis_access: Redis) -> None:
        """合法 access 消息经 handle_message 后进入 _pending 缓冲。"""
        from app.access.writer import AccessWriter

        writer = AccessWriter(redis_access)
        assert len(writer._pending) == 0

        msg = _make_mock_message()
        await writer.handle_message(msg)

        assert len(writer._pending) == 1

    async def test_handle_non_access_message_skipped(self, redis_access: Redis) -> None:
        """非 access log_type 消息不进入 _pending。"""
        import json

        from app.access.writer import AccessWriter

        writer = AccessWriter(redis_access)
        msg = MagicMock()
        msg.value = json.dumps({"log_type": "metrics", "aic": "x", "body": {}}).encode()
        msg.partition = 0
        msg.timestamp_type = 1
        msg.timestamp = 1000

        await writer.handle_message(msg)
        assert len(writer._pending) == 0

    async def test_batch_internal_dedup(self, redis_access: Redis) -> None:
        """同 log_id 两条消息，第二条被批内去重跳过。"""
        from app.access.writer import AccessWriter

        writer = AccessWriter(redis_access)
        log_id = str(uuid.uuid4())
        msg1 = _make_mock_message(log_id=log_id)
        msg2 = _make_mock_message(log_id=log_id)

        await writer.handle_message(msg1)
        await writer.handle_message(msg2)

        assert len(writer._pending) == 1


class TestEndToEndWithoutKafka:
    """D-1 端到端路径：handle_message → _flush_batch → CH + Redis（不依赖真实 Kafka）。"""

    async def test_full_pipeline_message_to_ch(self, redis_access: Redis) -> None:
        """从消息到 CH 写入的完整路径验证：handle_message + _flush_batch 均成功。"""
        from app.access.tables import ACCESS_EVENTS
        from app.access.writer import AccessWriter
        from app.core.clickhouse_client import get_clickhouse_client

        writer = AccessWriter(redis_access)
        log_id = str(uuid.uuid4())
        msg = _make_mock_message(log_id=log_id, aic="e2e-aic-001")

        # 1. 解析消息 → _pending
        await writer.handle_message(msg)
        assert len(writer._pending) == 1, "_pending 应有 1 条"

        # 2. flush → CH insert + watermark + dedupe mark
        result = await writer._flush_batch()
        assert result is True, "_flush_batch 应返回 True"

        # 3. 验证 CH 写入
        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        query_result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        count = query_result.result_rows[0][0]
        assert count == 1, f"CH 应有 1 行，实际 {count}"

    async def test_full_pipeline_trace_hint_updated(self, redis_access: Redis) -> None:
        """带 trace_id 的消息 flush 后，trace_hint 中应有该 trace_id（D-1）。"""
        from app.access.trace_hint import maybe_seen
        from app.access.writer import AccessWriter

        writer = AccessWriter(redis_access)
        trace_id = f"tr-e2e-{uuid.uuid4().hex[:8]}"
        msg = _make_mock_message(log_id=str(uuid.uuid4()))

        # 注入 trace_id 到消息（直接用 handle_message 后手动修改 pending）
        await writer.handle_message(msg)
        if writer._pending:
            # Override trace_id on the prepared record
            from dataclasses import replace

            writer._pending[0] = replace(writer._pending[0], trace_id=trace_id)

        result = await writer._flush_batch()
        assert result is True

        # trace_hint 应已更新（取决于配置 access_trace_seen_hint_enabled）
        from app.core.config import settings

        if settings.access_trace_seen_hint_enabled:
            seen = await maybe_seen(redis_access, trace_id)
            assert seen is True, "trace_hint 应已标记 trace_id"
