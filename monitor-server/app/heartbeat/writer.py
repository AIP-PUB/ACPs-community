"""Heartbeat Writer — Kafka Consumer → Redis 心跳写入器（§3.1）。

职责：
1. 消费 Kafka `amp.heartbeat` 主题
2. 提取 aic、observed_at_ms（LogAppendTime → observedTimestamp → DLQ）、source_timestamp_ms
3. 调用 hb_apply_heartbeat Redis Function 原子写入心跳当前态
4. 维护按输入分区的读模型新鲜度水位（§6.2.1）
5. 计数指标（accepted / ignored_older / enter_alive / refresh_alive）

C-SHARD-4：输入分区按 aic 分区（调用方 Producer 保证）。
C-TIME-3：observed_at_ms 来自 Kafka LogAppendTime 或 observedTimestamp（不用客户端本地时钟）。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from aiokafka.errors import KafkaError
from redis.asyncio import Redis

from app.core.config import settings
from app.core.kafka_consumer import BaseLogConsumer
from app.heartbeat.exception import HeartbeatConfigError, UntimedHeartbeatError
from app.heartbeat.functions import apply_heartbeat, ensure_functions_loaded
from app.heartbeat.metrics import metrics
from app.heartbeat.store import redis_now_ms, write_watermarks

logger = structlog.get_logger(__name__)

# Kafka timestamp type 常量（aiokafka 约定）
_LOG_APPEND_TIME = 1


class HeartbeatWriter(BaseLogConsumer):
    """Heartbeat LogRecord Kafka Consumer 与 Redis 写入处理器（§3.1）。

    继承 BaseLogConsumer：at-least-once、指数退避重试、DLQ 写入已由基类封装。
    覆写 start() 以加载 Redis Functions 并校验分区数（C-CONF-1）。
    覆写 run() 以使用 getmany 批量拉取并推进空闲水位（偏差 D-1）。
    覆写 _process_with_retry() 以短路 UntimedHeartbeatError 直达 DLQ（A-3）。

    Args:
        redis: 已初始化的 Redis 异步客户端（由调用方传入，Writer 不拥有生命周期）。
    """

    def __init__(self, redis: Redis) -> None:
        super().__init__(
            topic=settings.heartbeat_topic,
            dlq_topic=settings.heartbeat_dlq_topic,
            group_id=settings.heartbeat_consumer_group,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            security_protocol=settings.kafka_security_protocol,
            auto_offset_reset=settings.kafka_auto_offset_reset,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            heartbeat_interval_ms=settings.kafka_heartbeat_interval_ms,
        )
        self._redis = redis

        # 指标计数器
        self._accepted: int = 0
        self._ignored_older: int = 0
        self._enter_alive: int = 0
        self._refresh_alive: int = 0

        # 按分区水位：{partition: watermark_ms}（仅内存 max，定期 flush 到 Redis）
        self._partition_watermarks: dict[int, int] = {}
        self._last_watermark_flush_at: float = 0.0

    # ── 生命周期覆写 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 Consumer/DLQ Producer，加载 Redis Functions，校验分区数（A-1 / C-CONF-1）。

        Raises:
            HeartbeatConfigError: input_partition_count 与 topic 实际分区数不符。
        """
        await super().start()
        # 加载 Redis Functions（writer 启动时确保已就绪）
        from app.core.redis_client import get_redis

        await ensure_functions_loaded(get_redis())
        # C-CONF-1: 校验 heartbeat_input_partition_count == amp.heartbeat 实际分区数
        if self._consumer is not None:
            partitions = self._consumer.partitions_for_topic(self._topic)
            expected = settings.heartbeat_input_partition_count
            actual = len(partitions) if partitions else 0
            if actual > 0 and actual != expected:
                raise HeartbeatConfigError(
                    f"heartbeat_input_partition_count={expected} 与 "
                    f"topic '{self._topic}' 实际分区数 {actual} 不一致（C-CONF-1）"
                )

    async def run(self) -> None:
        """覆写基类 run()：getmany 批量拉取，每批一次 commit，批间推进空闲水位（A-1）。

        偏差 D-1: 高频心跳逐条 commit 吞吐不可接受；
        空闲水位推进需要 poll 边界与 assignment 信息，基类 async-for 模型不支持。
        """
        if self._consumer is None:
            raise RuntimeError("Consumer 未启动，请先调用 start()")

        while self._running:
            batches: dict[Any, list[Any]] = await self._consumer.getmany(
                timeout_ms=settings.heartbeat_writer_poll_timeout_ms
            )

            for _tp, messages in batches.items():
                for msg in messages:
                    success = await self._process_with_retry(msg)
                    if not success:
                        await self._send_to_dlq(msg)

            if batches:
                try:
                    await self._consumer.commit()
                    consumed = sum(len(m) for m in batches.values())
                    self._consumed_count += consumed
                    if self._consumed_count % 1000 < consumed:
                        logger.info(
                            "Kafka 消费进度",
                            topic=self._topic,
                            consumed=self._consumed_count,
                            errors=self._error_count,
                            dlq_writes=self._dlq_count,
                        )
                except KafkaError as exc:
                    logger.error(
                        "batch offset 提交失败，消息可能重复消费",
                        topic=self._topic,
                        error=str(exc),
                    )

            self._advance_idle_watermarks(batches)

    def _advance_idle_watermarks(self, batches: dict[Any, list[Any]]) -> None:
        """空闲分区水位推进（§6.2.1，A-1）。

        对 getmany 本批无消息、且已有水位记录的分区，将水位推进至当前墙钟时间，
        避免空闲分区被 freshness_evaluator 误判为滞后。

        Args:
            batches: getmany 返回的 {TopicPartition: [ConsumerRecord]} dict。
        """
        if self._consumer is None:
            return
        active_tps = set(batches.keys())
        now_ms = int(time.time() * 1000)
        for tp in self._consumer.assignment():
            if tp in active_tps:
                continue
            current = self._partition_watermarks.get(tp.partition)
            if current is not None and now_ms > current:
                self._partition_watermarks[tp.partition] = now_ms

    async def _process_with_retry(self, msg: Any) -> bool:
        """覆写基类重试逻辑：UntimedHeartbeatError 不可重试，直接写 DLQ（A-3）。

        其他异常保留基类指数退避行为。

        Args:
            msg: aiokafka ConsumerRecord 对象。

        Returns:
            True 表示消息已处理（含 DLQ 写入）；False 表示耗尽重试需外层写 DLQ。
        """
        delay = self._retry_base_delay_s
        for attempt in range(self._max_retries + 1):
            try:
                await self.handle_message(msg)
                return True
            except UntimedHeartbeatError as exc:
                # 不可重试：缺少时间戳，直接写 DLQ（跳过指数退避）
                self._error_count += 1
                logger.warning(
                    "消息缺少时间戳，跳过重试直接写入 DLQ",
                    topic=self._topic,
                    partition=getattr(msg, "partition", None),
                    offset=getattr(msg, "offset", None),
                    error=str(exc),
                )
                await self._send_to_dlq(msg)
                return True  # 已处理（DLQ），避免外层重复写 DLQ
            except Exception as exc:
                self._error_count += 1
                if attempt < self._max_retries:
                    logger.warning(
                        "消息处理失败，即将重试",
                        topic=self._topic,
                        partition=getattr(msg, "partition", None),
                        offset=getattr(msg, "offset", None),
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        retry_after_s=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
                else:
                    logger.error(
                        "消息处理失败，重试已耗尽",
                        topic=self._topic,
                        partition=getattr(msg, "partition", None),
                        offset=getattr(msg, "offset", None),
                        max_retries=self._max_retries,
                        error=str(exc),
                    )
        return False

    # ── 时间戳提取 ─────────────────────────────────────────────────────────────

    def _extract_timestamps(
        self,
        msg: Any,
        raw_dict: dict[str, Any],
    ) -> tuple[int | None, int | None]:
        """提取 observed_at_ms 与 source_timestamp_ms（§3.1 优先级规则）。

        observed_at_ms 优先级：
        1. Kafka LogAppendTime（timestamp_type == 1）— broker 时钟，最稳定
        2. LogRecord.observedTimestamp ISO 字符串（timestamp_type == 0 时回退）
        3. 两者均缺失 → None（调用方应将消息路由到 DLQ）

        source_timestamp_ms：LogRecord.timestamp ISO 字符串，纯诊断字段，可为 None。

        Args:
            msg: aiokafka ConsumerRecord。
            raw_dict: 已反序列化的 LogRecord JSON dict。

        Returns:
            (observed_at_ms, source_timestamp_ms) 二元组；无法提取时对应位置为 None。
        """
        observed_at_ms: int | None = None

        # 优先级 1：LogAppendTime（timestamp_type == 1 且时间戳非零）
        if getattr(msg, "timestamp_type", None) == _LOG_APPEND_TIME:
            ts = getattr(msg, "timestamp", 0)
            if ts and ts > 0:
                observed_at_ms = int(ts)

        # 优先级 2：LogRecord.observedTimestamp
        if observed_at_ms is None:
            observed_ts_str: str | None = raw_dict.get("observedTimestamp")
            if observed_ts_str:
                try:
                    dt = datetime.fromisoformat(observed_ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    observed_at_ms = int(dt.timestamp() * 1000)
                except ValueError, OverflowError:
                    pass

        # source_timestamp：LogRecord.timestamp（纯诊断，按毫秒）
        source_timestamp_ms: int | None = None
        source_ts_str: str | None = raw_dict.get("timestamp")
        if source_ts_str:
            try:
                dt = datetime.fromisoformat(source_ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                source_timestamp_ms = int(dt.timestamp() * 1000)
            except ValueError, OverflowError:
                pass

        return observed_at_ms, source_timestamp_ms

    # ── Kafka 消息处理 ─────────────────────────────────────────────────────────

    async def handle_message(self, message: Any) -> None:
        """处理单条 Heartbeat LogRecord Kafka 消息（at-least-once）。

        流程：
        1. 反序列化 JSON
        2. 跳过非 heartbeat logType
        3. 提取 observed_at_ms（失败则抛 UntimedHeartbeatError → 直接 DLQ，不重试）
        4. 调用 apply_heartbeat
        5. 更新计数器与分区水位
        6. 按间隔 flush 水位到 Redis

        Args:
            message: aiokafka ConsumerRecord。

        Raises:
            UntimedHeartbeatError: observed_at_ms 无法提取（触发直接 DLQ，跳过重试）。
            Exception: 其它处理错误，触发重试 + DLQ。
        """
        raw_value: bytes = getattr(message, "value", b"")
        if not raw_value:
            logger.debug("收到空 heartbeat 消息，跳过")
            return

        raw_dict: dict[str, Any] = json.loads(raw_value.decode("utf-8"))

        # 跳过非 heartbeat logType（兼容 Audit 等其它类型混投同 topic 的场景）
        log_type: str = raw_dict.get("logType", raw_dict.get("log_type", ""))
        if log_type != "heartbeat":
            logger.debug("非 heartbeat logType，跳过", log_type=log_type)
            return

        aic: str = raw_dict.get("aic", "")
        partition: int = getattr(message, "partition", 0)

        # 提取时间戳
        observed_at_ms, source_timestamp_ms = self._extract_timestamps(message, raw_dict)
        if observed_at_ms is None:
            raise UntimedHeartbeatError(
                f"无法提取 observed_at_ms，消息缺少 LogAppendTime 与 observedTimestamp，"
                f"aic={aic!r}, partition={partition}, offset={getattr(message, 'offset', None)}"
            )

        # 调用 Redis Function
        result = await apply_heartbeat(
            self._redis,
            aic=aic,
            observed_at_ms=observed_at_ms,
            source_timestamp_ms=source_timestamp_ms,
        )

        # 更新指标（含 HeartbeatMetrics，B-6）
        if result.status == "ignored_older":
            self._ignored_older += 1
            metrics.inc("amp_heartbeat_writer_ignored_older_total")
        else:
            self._accepted += 1
            metrics.inc("amp_heartbeat_writer_accepted_total")
            if result.kind == "enter_alive":
                self._enter_alive += 1
                metrics.inc("amp_heartbeat_writer_enter_alive_total")
            elif result.kind == "refresh_alive":
                self._refresh_alive += 1
                metrics.inc("amp_heartbeat_writer_refresh_alive_total")

        # 更新分区水位（单调递增，§6.2.1）
        # ignored_older 也推进水位：该消息已被消费，分区进度确实推进了
        current = self._partition_watermarks.get(partition, 0)
        if observed_at_ms > current:
            self._partition_watermarks[partition] = observed_at_ms

        # 按间隔 flush 水位到 Redis
        flush_interval_ms = settings.heartbeat_writer_watermark_flush_interval_ms
        if time.monotonic() - self._last_watermark_flush_at >= flush_interval_ms / 1000.0:
            await self._flush_watermarks()

    # ── 水位 flush ─────────────────────────────────────────────────────────────

    async def _flush_watermarks(self) -> None:
        """将内存水位 flush 到 Redis Hash（§6.2.1，§4.2 WATERMARKS_KEY）。

        entries 格式：{partition: (watermark_ms, updated_at_ms)}。
        updated_at_ms 使用 Redis TIME（A-2 / C-TIME-3），仅在有水位数据时写入。
        """
        if not self._partition_watermarks:
            return

        now_ms = await redis_now_ms(self._redis)  # A-2: 使用 Redis TIME（C-TIME-3）
        entries: dict[int, tuple[int, int]] = {
            partition: (watermark_ms, now_ms) for partition, watermark_ms in self._partition_watermarks.items()
        }
        await write_watermarks(self._redis, entries)
        self._last_watermark_flush_at = time.monotonic()
        logger.debug(
            "Heartbeat Writer 水位已 flush",
            partition_count=len(entries),
        )
