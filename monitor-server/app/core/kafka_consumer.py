"""基础 Kafka Consumer 抽象。

提供可被各日志 Writer 继承的异步 Consumer 基类，封装 aiokafka 生命周期管理、
at-least-once 语义、指数退避重试以及 DLQ 写入逻辑。

各日志类型（Audit、Heartbeat 等）应子类化 BaseLogConsumer，实现 handle_message()。
"""

from __future__ import annotations

import abc
import asyncio
import json
from typing import Any

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BASE_DELAY_S = 0.5  # 首次重试等待 0.5s，后续指数增长


class BaseLogConsumer(abc.ABC):
    """异步 Kafka Consumer 基类。

    实现 at-least-once 语义：每条消息处理成功后方提交 offset。
    处理失败时采用指数退避重试，耗尽重试次数后将消息写入 DLQ。

    子类实现 handle_message() 以处理具体业务逻辑。

    Args:
        topic: 订阅的 Kafka 主题名称。
        dlq_topic: 死信队列主题名称。
        group_id: Consumer group ID。
        bootstrap_servers: Kafka bootstrap servers 地址。
        security_protocol: 安全协议（PLAINTEXT / SSL / SASL_SSL）。
        auto_offset_reset: 初次消费或 offset 丢失时的策略（earliest / latest）。
        max_poll_records: 单次 poll 最大消息条数。
        session_timeout_ms: Consumer 会话超时（毫秒）。
        heartbeat_interval_ms: Consumer 心跳间隔（毫秒）。
        max_retries: 消息处理失败时的最大重试次数。
        retry_base_delay_s: 首次重试等待基础秒数（后续指数翻倍）。
    """

    def __init__(
        self,
        topic: str,
        dlq_topic: str,
        group_id: str,
        bootstrap_servers: str,
        security_protocol: str = "PLAINTEXT",
        auto_offset_reset: str = "earliest",
        max_poll_records: int = 100,
        session_timeout_ms: int = 30000,
        heartbeat_interval_ms: int = 10000,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_base_delay_s: float = _DEFAULT_RETRY_BASE_DELAY_S,
    ) -> None:
        self._topic = topic
        self._dlq_topic = dlq_topic
        self._group_id = group_id
        self._bootstrap_servers = bootstrap_servers
        self._security_protocol = security_protocol
        self._auto_offset_reset = auto_offset_reset
        self._max_poll_records = max_poll_records
        self._session_timeout_ms = session_timeout_ms
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._max_retries = max_retries
        self._retry_base_delay_s = retry_base_delay_s
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._running = False
        self._consumed_count = 0
        self._error_count = 0
        self._dlq_count = 0

    async def start(self) -> None:
        """启动 Kafka Consumer 和 DLQ Producer，开始订阅主题。

        Raises:
            KafkaError: Kafka 连接或订阅失败时抛出。
        """
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            security_protocol=self._security_protocol,
            enable_auto_commit=False,
            auto_offset_reset=self._auto_offset_reset,
            max_partition_fetch_bytes=1048576,
            session_timeout_ms=self._session_timeout_ms,
            heartbeat_interval_ms=self._heartbeat_interval_ms,
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            security_protocol=self._security_protocol,
        )
        await self._consumer.start()
        await self._producer.start()
        self._running = True
        logger.info(
            "Kafka consumer 已启动",
            topic=self._topic,
            dlq_topic=self._dlq_topic,
            group_id=self._group_id,
            bootstrap_servers=self._bootstrap_servers,
        )

    async def stop(self) -> None:
        """停止 Kafka Consumer 和 DLQ Producer，释放资源。"""
        self._running = False
        if self._consumer is not None:
            await self._consumer.stop()
        if self._producer is not None:
            await self._producer.stop()
        logger.info(
            "Kafka consumer 已停止",
            topic=self._topic,
            consumed=self._consumed_count,
            errors=self._error_count,
            dlq_writes=self._dlq_count,
        )

    async def run(self) -> None:
        """持续消费消息的主循环（at-least-once 语义）。

        流程：消费消息 → 处理（含重试）→ 提交 offset。
        处理失败超过重试上限时，写入 DLQ 后提交 offset，不阻塞主循环。
        """
        if self._consumer is None:
            raise RuntimeError("Consumer 未启动，请先调用 start()")

        async for msg in self._consumer:
            if not self._running:
                break

            success = await self._process_with_retry(msg)
            if not success:
                await self._send_to_dlq(msg)

            try:
                await self._consumer.commit()
                self._consumed_count += 1
                if self._consumed_count % 1000 == 0:
                    logger.info(
                        "Kafka 消费进度",
                        topic=self._topic,
                        consumed=self._consumed_count,
                        errors=self._error_count,
                        dlq_writes=self._dlq_count,
                    )
            except KafkaError as exc:
                logger.error(
                    "offset 提交失败，消息可能重复消费",
                    topic=self._topic,
                    partition=msg.partition,
                    offset=msg.offset,
                    error=str(exc),
                )

    async def _process_with_retry(self, msg: Any) -> bool:
        """尝试处理消息，失败时按指数退避重试。

        Args:
            msg: aiokafka ConsumerRecord 对象。

        Returns:
            True 表示最终处理成功，False 表示耗尽重试次数。
        """
        delay = self._retry_base_delay_s
        for attempt in range(self._max_retries + 1):
            try:
                await self.handle_message(msg)
                return True
            except Exception as exc:
                self._error_count += 1
                if attempt < self._max_retries:
                    logger.warning(
                        "消息处理失败，即将重试",
                        topic=self._topic,
                        partition=msg.partition,
                        offset=msg.offset,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        retry_after_s=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
                else:
                    logger.error(
                        "消息处理失败，重试已耗尽，写入 DLQ",
                        topic=self._topic,
                        partition=msg.partition,
                        offset=msg.offset,
                        max_retries=self._max_retries,
                        error=str(exc),
                    )
        return False

    async def _send_to_dlq(self, msg: Any) -> None:
        """将处理失败的消息写入 DLQ 主题。

        Args:
            msg: aiokafka ConsumerRecord 对象。
        """
        if self._producer is None:
            logger.error("DLQ producer 未初始化，消息丢失", topic=self._dlq_topic)
            return

        dlq_payload = {
            "source_topic": msg.topic,
            "source_partition": msg.partition,
            "source_offset": msg.offset,
            "key": msg.key.decode("utf-8", errors="replace") if msg.key else None,
            "value": msg.value.decode("utf-8", errors="replace") if msg.value else None,
        }
        try:
            await self._producer.send_and_wait(
                self._dlq_topic,
                value=json.dumps(dlq_payload).encode("utf-8"),
            )
            self._dlq_count += 1
            logger.info(
                "消息已写入 DLQ",
                dlq_topic=self._dlq_topic,
                source_partition=msg.partition,
                source_offset=msg.offset,
            )
        except KafkaError as exc:
            logger.error(
                "DLQ 写入失败，消息永久丢失",
                dlq_topic=self._dlq_topic,
                source_partition=msg.partition,
                source_offset=msg.offset,
                error=str(exc),
            )

    @abc.abstractmethod
    async def handle_message(self, message: Any) -> None:
        """处理单条 Kafka 消息。子类实现具体业务逻辑。

        Args:
            message: aiokafka ConsumerRecord 对象。

        Raises:
            Exception: 处理失败时抛出，触发重试和 DLQ 逻辑。
        """
