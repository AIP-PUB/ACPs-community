"""app/system/writer.py — SystemWriter(BaseLogConsumer)。设计 §3.1、§10.1 写入路径。

消费 amp.system → 取/兜底 log_id → normalizer.build_document → 攒批 →
Bulk Index(_id=log_id, upsert) → 推保守摄取水位 → commit。
无 dedupe：OpenSearch _id 唯一性即去重（C-SYSTEM-WRITE-6），区别于 access/message 的 Redis 去重窗口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import structlog
from acps_sdk.amp.models import LogRecord, compute_log_id_fallback
from pydantic import ValidationError

from app.core.config import settings
from app.core.kafka_consumer import BaseLogConsumer
from app.system import freshness, store
from app.system.exception import InvalidSystemRecordError, OpenSearchBulkError
from app.system.metrics import (
    AMP_SYSTEM_BULK_INDEX_FAILURES_TOTAL,
    AMP_SYSTEM_BULK_INDEX_LATENCY_MS,
    AMP_SYSTEM_WRITER_ACCEPTED_TOTAL,
    AMP_SYSTEM_WRITER_DLQ_TOTAL,
    AMP_SYSTEM_WRITER_NORMALIZED_TOTAL,
    metrics,
)
from app.system.normalizer import SystemEventDoc, build_document

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _PreparedDoc:
    """单条消息预处理结果（入攒批缓冲前的产物）。"""

    partition: int
    timestamp_ms: int
    doc: SystemEventDoc
    raw_msg: Any  # 原始 AIOKafka message 对象，permanent bulk 失败时投 DLQ 用


class SystemWriter(BaseLogConsumer):
    """消费 amp.system，规范化 → 攒批 Bulk Index(_id=log_id) → 推保守水位 → commit。

    不变式：_pending 始终对应「已消费但未提交 offset」的消息。
    commit 在 _flush_batch 成功后由 run() 调用。
    无 dedupe：OpenSearch _id upsert 保证幂等（C-SYSTEM-WRITE-6 / 设计 §2 决策 3）。
    """

    def __init__(self, redis: Any) -> None:
        super().__init__(
            topic=settings.system_topic,
            dlq_topic=settings.system_dlq_topic,
            group_id=settings.system_consumer_group,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            security_protocol=settings.kafka_security_protocol,
            auto_offset_reset=settings.kafka_auto_offset_reset,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            heartbeat_interval_ms=settings.kafka_heartbeat_interval_ms,
        )
        self._redis = redis
        self._pending: list[_PreparedDoc] = []

    async def handle_message(self, message: Any) -> None:
        """per-message 解析 + 规范化（不做 durable 写，只入攒批缓冲）。"""
        if not message.value:
            return
        raw_dict = json.loads(message.value)
        if raw_dict.get("log_type") != "system":
            return
        record = LogRecord.model_validate(raw_dict)
        log_id = record.log_id or compute_log_id_fallback(raw_dict)
        doc = build_document(
            record,
            log_id=log_id,
            search_text_max_length=settings.system_search_text_max_length,
        )
        self._pending.append(
            _PreparedDoc(
                partition=message.partition,
                timestamp_ms=doc.timestamp_ms,
                doc=doc,
                raw_msg=message,
            )
        )
        metrics.inc(AMP_SYSTEM_WRITER_NORMALIZED_TOTAL)

    async def _process_with_retry(self, msg: Any) -> bool:
        """覆写：InvalidSystemRecordError / JSONDecodeError / ValidationError → 不重试 → DLQ。"""
        try:
            await self.handle_message(msg)
            return True
        except InvalidSystemRecordError, json.JSONDecodeError, ValidationError:
            logger.warning(
                "system: bad record → DLQ (no retry)",
                exc_info=True,
                value_preview=str(msg.value)[:200] if msg.value else None,
            )
            await self._send_to_dlq(msg)
            metrics.inc(AMP_SYSTEM_WRITER_DLQ_TOTAL)
            return False
        except Exception:
            logger.exception("system: transient error in handle_message")
            raise

    async def run(self) -> None:
        """覆写（D-1）：「5s 或 5000 docs」攒批 Bulk，flush 成功后才 commit offset。

        使用 getmany(timeout_ms=system_writer_poll_timeout_ms) 实现时间触发与消息触发双驱动，
        即使无新消息到达也能按时间间隔刷新批次（对齐 message/metrics writer 模式）。
        """
        assert self._consumer is not None, "SystemWriter.run() called before start()"  # nosec B101
        consumer = self._consumer
        batch_interval = settings.system_bulk_index_batch_interval_seconds
        batch_max_docs = settings.system_bulk_index_batch_max_docs
        poll_timeout_ms = settings.system_writer_poll_timeout_ms
        last_flush = monotonic()

        while True:
            msgs = await consumer.getmany(
                timeout_ms=poll_timeout_ms,
                max_records=batch_max_docs,
            )
            for _tp, batch in msgs.items():
                for msg in batch:
                    await self._process_with_retry(msg)

            now = monotonic()
            elapsed = now - last_flush
            due = elapsed >= batch_interval

            if self._pending and (len(self._pending) >= batch_max_docs or due):
                ok = await self._flush_batch()
                if ok:
                    await consumer.commit()
                else:
                    await self._seek_to_committed()
                self._pending.clear()
                last_flush = monotonic()

            await self._advance_idle_partitions()

    async def _flush_batch(self) -> bool:
        """无 dedupe，直接 Bulk Index（C-SYSTEM-WRITE-6：_id=log_id upsert 即幂等）。

        transient 全失败 → return False（整批重试）；
        permanent 失败项 → DLQ；成功项记指标。
        """
        indexed_at_iso = datetime.now(UTC).isoformat()
        t0 = monotonic()
        try:
            result = await store.bulk_index(
                [p.doc for p in self._pending],
                indexed_at_iso=indexed_at_iso,
            )
        except OpenSearchBulkError:
            metrics.inc(AMP_SYSTEM_BULK_INDEX_FAILURES_TOTAL)
            logger.warning("system: bulk transient failure, will retry batch")
            return False
        finally:
            elapsed_ms = (monotonic() - t0) * 1000
            metrics.observe(AMP_SYSTEM_BULK_INDEX_LATENCY_MS, elapsed_ms)

        # permanent 失败项投 DLQ
        for log_id, reason in result.failed_items:
            matching = next((p for p in self._pending if p.doc.log_id == log_id), None)
            if matching:
                await self._send_to_dlq(matching.raw_msg)
            metrics.inc(AMP_SYSTEM_WRITER_DLQ_TOTAL)
            logger.warning("system: permanent bulk failure → DLQ", log_id=log_id, reason=reason)

        # 推进每分区保守水位
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        partition_max: dict[int, int] = {}
        for p in self._pending:
            partition_max[p.partition] = max(partition_max.get(p.partition, 0), p.timestamp_ms)

        for partition_id, max_ts_ms in partition_max.items():
            await freshness.advance_partition_watermark(
                self._redis,
                partition_id=partition_id,
                batch_max_event_ts_ms=max_ts_ms,
                now_ms=now_ms,
                reorder_margin_ms=settings.system_freshness_reorder_margin_ms,
            )

        metrics.inc(AMP_SYSTEM_WRITER_ACCEPTED_TOTAL, result.indexed)
        logger.info(
            "system: bulk indexed",
            indexed=result.indexed,
            failed=len(result.failed_items),
        )
        return True

    async def _seek_to_committed(self) -> None:
        """Bulk 持续 transient 失败时，将 consumer 各分区 seek 回已提交 offset。

        幂等安全：OpenSearch _id upsert 保证重放不产生重复文档（C-SYSTEM-WRITE-6）。
        """
        if self._consumer is None:
            return
        try:
            for tp in self._consumer.assignment():
                committed = await self._consumer.committed(tp)
                if committed is not None:
                    self._consumer.seek(tp, committed)
        except Exception:
            logger.warning("system: _seek_to_committed failed", exc_info=True)

    async def _advance_idle_partitions(self) -> None:
        """分区追平 highwater 无新事件时，保守水位向 (now-margin) 收敛（防冻结）。"""
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
                        reorder_margin_ms=settings.system_freshness_reorder_margin_ms,
                    )
        except Exception:
            logger.debug("system: _advance_idle_partitions error, skipping", exc_info=True)
