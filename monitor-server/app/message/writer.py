"""app/message/writer.py — MessageWriter(BaseLogConsumer)。

实现设计 §3.1、§10.1 写入路径：
  消费 amp.message → 取/兜底 log_id → 解析 MessageBody → 计算 lifecycle_key →
  规范化成 EventRow → 批内内存去重 → 攒批 →
  写前去重检查 → CH insert (message_events) → 推进每分区水位 → 写去重标记 → commit。

三步提交顺序（CH → 标记 → offset，C-MESSAGE-WRITE-1/2）。
只写 message_events 主表，不碰任何派生表（派生交 compactor）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import structlog
from acps_sdk.amp.models import LogRecord, MessageBody, compute_log_id_fallback
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.config import settings
from app.core.kafka_consumer import BaseLogConsumer
from app.message import dedupe, freshness, store
from app.message.events import EventRow, build_event_row
from app.message.exception import ClickHouseInsertError, InvalidMessageRecordError
from app.message.lifecycle_key import compute_lifecycle_key

logger = structlog.get_logger(__name__)

_LOG_APPEND_TIME = 1  # aiokafka timestamp_type for LogAppendTime


@dataclass(frozen=True)
class _PreparedRecord:
    """单条消息预处理结果（入攒批缓冲前的产物）。"""

    log_id: str
    partition: int
    timestamp_ms: int
    row: EventRow


class MessageWriter(BaseLogConsumer):
    """消费 amp.message，去重/规范化 → 攒批 CH insert → 推水位 → commit。

    不变式：_pending 始终对应「已消费但未提交 offset」的消息。
    commit 在 _flush_batch 成功后由 run() 调用（C-MESSAGE-WRITE-1）。
    """

    def __init__(self, redis: Redis) -> None:
        super().__init__(
            topic=settings.message_topic,
            dlq_topic=settings.message_dlq_topic,
            group_id=settings.message_consumer_group,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            security_protocol=settings.kafka_security_protocol,
            auto_offset_reset=settings.kafka_auto_offset_reset,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            heartbeat_interval_ms=settings.kafka_heartbeat_interval_ms,
        )
        self._redis = redis
        self._pending: list[_PreparedRecord] = []
        self._batch_log_ids: set[str] = set()

    async def handle_message(self, message: Any) -> None:
        """per-message 解析 + 成行（不做 durable 写，只入攒批缓冲）。"""
        if not message.value:
            return

        raw_dict = json.loads(message.value)

        if raw_dict.get("log_type") != "message":
            return

        record = LogRecord.model_validate(raw_dict)
        log_id = record.log_id or compute_log_id_fallback(raw_dict)

        # 批内内存去重
        if log_id in self._batch_log_ids:
            return

        body = MessageBody.model_validate(record.body or {})
        observed_at_ms = self._extract_observed_at(message, record)

        lifecycle_key = compute_lifecycle_key(
            message_id=getattr(body, "message_id", None),
            correlation_id=record.correlation_id,
            correlation_id_stable_unique=settings.message_correlation_id_stable_unique,
        )

        row = build_event_row(
            record=record,
            body=body,
            log_id=log_id,
            lifecycle_key=lifecycle_key,
            observed_at_ms=observed_at_ms,
            store_raw_log=settings.message_raw_log_enabled,
        )

        self._batch_log_ids.add(log_id)
        self._pending.append(
            _PreparedRecord(
                log_id=log_id,
                partition=message.partition,
                timestamp_ms=row.timestamp_ms,
                row=row,
            )
        )

    async def _process_with_retry(self, msg: Any) -> bool:
        """覆写：InvalidMessageRecordError / JSON / Pydantic 错误短路 False（不重试）。"""
        try:
            await self.handle_message(msg)
            return True
        except InvalidMessageRecordError, json.JSONDecodeError, ValidationError:
            logger.warning(
                "Message record invalid, sending to DLQ",
                topic=self._topic,
                partition=getattr(msg, "partition", None),
                offset=getattr(msg, "offset", None),
                exc_info=True,
            )
            return False
        except Exception:
            return await super()._process_with_retry(msg)

    async def run(self) -> None:
        """覆写：攒批 + 三步提交循环（C-MESSAGE-WRITE-1/2）。"""
        assert self._consumer is not None, "MessageWriter.run() called before start()"  # nosec B101
        consumer = self._consumer
        last_flush = monotonic()
        while self._running:
            batches = await consumer.getmany(timeout_ms=settings.message_writer_poll_timeout_ms)
            for _tp, msgs in batches.items():
                for msg in msgs:
                    if not await self._process_with_retry(msg):
                        await self._send_to_dlq(msg)

            due = monotonic() - last_flush >= settings.message_insert_batch_interval_seconds
            if self._pending and (len(self._pending) >= settings.message_insert_batch_max_rows or due):
                if await self._flush_batch():
                    await consumer.commit()
                else:
                    await self._seek_to_committed()
                self._pending = []
                self._batch_log_ids.clear()
                last_flush = monotonic()

            await self._advance_idle_partitions()

    async def _flush_batch(self) -> bool:
        """返回 True=成功（run 提交 offset），False=CH 失败（run seek 回退）。"""
        if not self._pending:
            return True

        log_ids = [p.log_id for p in self._pending]
        unseen, available = await dedupe.filter_unseen(self._redis, log_ids)
        if not available:
            logger.warning("MessageWriter: Redis dedupe unavailable, writing all")

        kept = [p for p in self._pending if p.log_id in unseen]
        if not kept:
            return True

        try:
            await store.insert_events([p.row for p in kept])
        except ClickHouseInsertError:
            logger.error("MessageWriter: CH insert failed, will retry batch", exc_info=True)
            return False

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        from collections import defaultdict

        partition_max_ts: dict[int, int] = defaultdict(int)
        for p in kept:
            if p.timestamp_ms > partition_max_ts[p.partition]:
                partition_max_ts[p.partition] = p.timestamp_ms

        for part, max_ts in partition_max_ts.items():
            await freshness.advance_partition_watermark(
                self._redis,
                partition_id=part,
                batch_max_ts_ms=max_ts,
                now_ms=now_ms,
            )

        ttl_secs = settings.message_dedup_window_seconds
        await dedupe.mark_seen(self._redis, [p.log_id for p in kept], ttl_seconds=ttl_secs)

        return True

    async def _seek_to_committed(self) -> None:
        """CH 失败时 seek 回已提交 offset。"""
        if self._consumer is None:
            return
        for tp in self._consumer.assignment():
            committed = await self._consumer.committed(tp)
            if committed is not None:
                self._consumer.seek(tp, committed)

    async def _advance_idle_partitions(self) -> None:
        """空闲分区水位推进（lag=0 时防水位冻结）。"""
        if self._consumer is None:
            return
        try:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            for tp in self._consumer.assignment():
                pos = await self._consumer.position(tp)
                hw = self._consumer.highwater(tp)
                if hw is not None and pos >= hw:
                    await freshness.advance_idle_partition(
                        self._redis,
                        partition_id=tp.partition,
                        now_ms=now_ms,
                    )
        except Exception:
            logger.debug("_advance_idle_partitions: error, skipping", exc_info=True)

    def _extract_observed_at(self, message: Any, record: LogRecord) -> int:
        """采集时间优先级（设计 §2.4）：LogAppendTime > observed_timestamp > now。"""
        if getattr(message, "timestamp_type", None) == _LOG_APPEND_TIME:
            ts = getattr(message, "timestamp", None)
            if ts:
                return int(ts)
        if record.observed_timestamp:
            try:
                dt = datetime.fromisoformat(record.observed_timestamp.replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except ValueError, AttributeError:
                pass
        return int(datetime.now(UTC).timestamp() * 1000)
