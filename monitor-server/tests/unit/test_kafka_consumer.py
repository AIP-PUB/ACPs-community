"""tests/unit/test_kafka_consumer.py — BaseLogConsumer 单元测试（mock aiokafka）。

覆盖：
- _process_with_retry：首次成功、重试后成功、耗尽重试
- _send_to_dlq：正常写入、无 producer 时优雅降级
- run 循环：_running=False 时退出
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.kafka_consumer import BaseLogConsumer


class _ConcreteConsumer(BaseLogConsumer):
    """最小具体子类，供测试实例化。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            topic="test.topic",
            dlq_topic="test.topic.dlq",
            group_id="test-group",
            bootstrap_servers="localhost:19092",
            **kwargs,
        )
        self.handle_calls: list[Any] = []

    async def handle_message(self, message: Any) -> None:
        self.handle_calls.append(message)


def _make_mock_msg(
    value: bytes = b'{"log_type": "audit"}',
    partition: int = 0,
    offset: int = 0,
    key: bytes | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.value = value
    msg.key = key
    msg.topic = "test.topic"
    msg.partition = partition
    msg.offset = offset
    return msg


class TestProcessWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt_returns_true(self) -> None:
        consumer = _ConcreteConsumer()
        msg = _make_mock_msg()
        result = await consumer._process_with_retry(msg)
        assert result is True
        assert len(consumer.handle_calls) == 1

    @pytest.mark.asyncio
    async def test_success_after_one_failure_returns_true(self) -> None:
        consumer = _ConcreteConsumer(max_retries=3, retry_base_delay_s=0.0)
        msg = _make_mock_msg()

        call_count = 0

        async def flaky_handle(message: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("临时错误")

        consumer.handle_message = flaky_handle  # type: ignore[method-assign]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await consumer._process_with_retry(msg)

        assert result is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_false(self) -> None:
        consumer = _ConcreteConsumer(max_retries=2, retry_base_delay_s=0.0)
        msg = _make_mock_msg()

        async def always_fail(message: Any) -> None:
            raise RuntimeError("持续失败")

        consumer.handle_message = always_fail  # type: ignore[method-assign]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await consumer._process_with_retry(msg)

        assert result is False
        assert consumer._error_count == 3  # max_retries=2 → 共 3 次尝试

    @pytest.mark.asyncio
    async def test_error_count_increments_on_each_failure(self) -> None:
        consumer = _ConcreteConsumer(max_retries=1, retry_base_delay_s=0.0)
        msg = _make_mock_msg()

        async def always_fail(message: Any) -> None:
            raise ValueError("错误")

        consumer.handle_message = always_fail  # type: ignore[method-assign]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await consumer._process_with_retry(msg)

        assert consumer._error_count == 2


class TestSendToDlq:
    @pytest.mark.asyncio
    async def test_dlq_message_sent_when_producer_available(self) -> None:
        consumer = _ConcreteConsumer()
        msg = _make_mock_msg(value=b"bad-payload", partition=1, offset=42)

        mock_producer = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()
        consumer._producer = mock_producer

        await consumer._send_to_dlq(msg)

        mock_producer.send_and_wait.assert_awaited_once()
        call_args = mock_producer.send_and_wait.call_args
        assert call_args.args[0] == "test.topic.dlq"
        sent_payload = json.loads(call_args.kwargs["value"].decode())
        assert sent_payload["source_partition"] == 1
        assert sent_payload["source_offset"] == 42
        assert consumer._dlq_count == 1

    @pytest.mark.asyncio
    async def test_dlq_graceful_when_producer_is_none(self) -> None:
        consumer = _ConcreteConsumer()
        msg = _make_mock_msg()
        consumer._producer = None

        await consumer._send_to_dlq(msg)  # 不应抛异常

        assert consumer._dlq_count == 0

    @pytest.mark.asyncio
    async def test_dlq_count_increments_on_success(self) -> None:
        consumer = _ConcreteConsumer()
        msg = _make_mock_msg()

        mock_producer = AsyncMock()
        consumer._producer = mock_producer

        await consumer._send_to_dlq(msg)
        assert consumer._dlq_count == 1

    @pytest.mark.asyncio
    async def test_dlq_kafka_error_does_not_raise(self) -> None:
        from aiokafka.errors import KafkaError

        consumer = _ConcreteConsumer()
        msg = _make_mock_msg()
        mock_producer = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(side_effect=KafkaError("kafka 连接失败"))
        consumer._producer = mock_producer

        await consumer._send_to_dlq(msg)  # 不应向外抛异常

        assert consumer._dlq_count == 0


class TestStopClearsRunningFlag:
    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self) -> None:
        consumer = _ConcreteConsumer()
        consumer._running = True

        mock_kafka = AsyncMock()
        consumer._consumer = mock_kafka
        consumer._producer = AsyncMock()

        await consumer.stop()

        assert consumer._running is False
        mock_kafka.stop.assert_awaited_once()
