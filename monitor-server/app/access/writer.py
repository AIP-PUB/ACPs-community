"""app/access/writer.py — AccessWriter(BaseLogConsumer)。

实现设计 §3.1、§10.1 写入路径：
  消费 → 取/兜底 log_id → 规范化/脱敏成行 → 批内内存去重 → 攒批 →
  写前去重检查 → CH insert → 推进每分区水位 → 写去重标记 → 可选 hint → commit。

三步提交顺序（CH → 标记 → offset，C-ACCESS-WRITE-7）。
偏异 D-1：覆写 run() 实现攒批循环；InvalidAccessRecordError 不重试直达 DLQ。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import structlog
from acps_sdk.amp.models import AccessBody, LogRecord, compute_log_id_fallback
from pydantic import ValidationError
from redis.asyncio import Redis

from app.access import dedupe, freshness, store, trace_hint
from app.access.events import EventRow, build_event_row
from app.access.exception import ClickHouseInsertError, InvalidAccessRecordError
from app.access.metrics import metrics as access_metrics
from app.access.redaction import parse_allowlist
from app.core.config import settings
from app.core.kafka_consumer import BaseLogConsumer

logger = structlog.get_logger(__name__)

_LOG_APPEND_TIME = 1  # aiokafka timestamp_type for LogAppendTime


@dataclass(frozen=True)
class _PreparedRecord:
    """单条消息预处理结果（入攒批缓冲前的产物）。"""

    log_id: str
    partition: int
    timestamp_ms: int
    trace_id: str
    row: EventRow
    redacted_headers: int


class AccessWriter(BaseLogConsumer):
    """消费 amp.access，去重/规范/脱敏 → 攒批 CH insert → 推水位 → commit。

    不变式：_pending 始终对应"已消费但未提交 offset"的消息。
    commit 在 _flush_batch 成功后由 run() 调用（C-ACCESS-WRITE-7）。
    """

    def __init__(self, redis: Redis) -> None:
        super().__init__(
            topic=settings.access_topic,
            dlq_topic=settings.access_dlq_topic,
            group_id=settings.access_consumer_group,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            security_protocol=settings.kafka_security_protocol,
            auto_offset_reset=settings.kafka_auto_offset_reset,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            heartbeat_interval_ms=settings.kafka_heartbeat_interval_ms,
        )
        self._redis = redis
        self._allowlist = parse_allowlist(settings.access_redacted_header_allowlist)
        self._pending: list[_PreparedRecord] = []
        self._batch_log_ids: set[str] = set()

    # 不覆写 start()：直接继承基类。

    async def handle_message(self, message: Any) -> None:
        """per-message 解析 + 成行（不做 durable 写，只入攒批缓冲）。"""
        if not message.value:
            return

        raw_dict = json.loads(message.value)

        # 快速跳过非 access 类型（避免对不完整消息做全量 LogRecord 校验）
        if raw_dict.get("log_type") != "access":
            return

        record = LogRecord.model_validate(raw_dict)

        log_id = record.log_id or compute_log_id_fallback(raw_dict)

        # 批内内存去重（防同窗口内重复绕过未写的持久化标记）
        if log_id in self._batch_log_ids:
            return

        body = AccessBody.model_validate(record.body or {})
        observed_at_ms = self._extract_observed_at(message, record)

        row, redacted_count = build_event_row(
            record=record,
            body=body,
            log_id=log_id,
            observed_at_ms=observed_at_ms,
            allowlist=self._allowlist,
            store_raw_log=settings.access_raw_log_enabled,
        )

        self._batch_log_ids.add(log_id)
        self._pending.append(
            _PreparedRecord(
                log_id=log_id,
                partition=message.partition,
                timestamp_ms=row.timestamp_ms,
                trace_id=row.trace_id,
                row=row,
                redacted_headers=redacted_count,
            )
        )

    async def _process_with_retry(self, msg: Any) -> bool:
        """覆写：InvalidAccessRecordError / JSON / Pydantic 错误短路返回 False（不重试）。"""
        try:
            await self.handle_message(msg)
            return True
        except InvalidAccessRecordError, json.JSONDecodeError, ValidationError:
            logger.warning(
                "Access record invalid, sending to DLQ",
                topic=self._topic,
                partition=getattr(msg, "partition", None),
                offset=getattr(msg, "offset", None),
                exc_info=True,
            )
            return False
        except Exception:
            # Other exceptions fall through to super() with retry
            return await super()._process_with_retry(msg)

    async def run(self) -> None:
        """覆写（偏异 D-1）：攒批 + 三步提交循环（C-ACCESS-WRITE-7）。"""
        assert self._consumer is not None, "AccessWriter.run() called before start()"  # nosec B101
        consumer = self._consumer  # 局部绑定，mypy 可以窄化类型
        last_flush = monotonic()
        while self._running:
            batches = await consumer.getmany(timeout_ms=settings.access_writer_poll_timeout_ms)
            for _tp, msgs in batches.items():
                for msg in msgs:
                    if not await self._process_with_retry(msg):
                        await self._send_to_dlq(msg)

            due = monotonic() - last_flush >= settings.access_insert_batch_interval_seconds
            if self._pending and (len(self._pending) >= settings.access_insert_batch_max_rows or due):
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

        # 1. 写前只读去重检查（fail-open）
        log_ids = [p.log_id for p in self._pending]
        unseen, available = await dedupe.filter_unseen(self._redis, log_ids)
        if not available:
            # Redis 不可用时 fail-open（继续写），但计数
            access_metrics.inc("amp_access_writer_dedup_unavailable_total")

        kept = [p for p in self._pending if p.log_id in unseen]
        if not kept:
            return True

        # 统计去重剔除数与脱敏头数
        deduped_count = len(self._pending) - len(kept)
        if deduped_count > 0:
            access_metrics.inc("amp_access_writer_deduped_total", by=deduped_count)

        total_redacted = sum(p.redacted_headers for p in kept)
        if total_redacted > 0:
            access_metrics.inc("amp_access_writer_redacted_headers_total", by=total_redacted)

        # accepted_total：去重后实际入库的消息数（设计 §6.19）
        access_metrics.inc("amp_access_writer_accepted_total", by=len(kept))

        # 2. 三步提交第 1 步：CH insert（测量延迟）
        t0 = monotonic()
        try:
            await store.insert_events([p.row for p in kept])
        except ClickHouseInsertError:
            logger.error("AccessWriter: CH insert failed, will retry batch", exc_info=True)
            access_metrics.inc("amp_access_insert_failures_total")
            return False
        finally:
            access_metrics.observe("amp_access_insert_latency_ms", (monotonic() - t0) * 1000)

        # 3. CH 成功后推进水位 + 写去重标记 + 可选 hint
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        from collections import defaultdict

        partition_max_ts: dict[int, int] = defaultdict(int)
        trace_ids: set[str] = set()
        for p in kept:
            if p.timestamp_ms > partition_max_ts[p.partition]:
                partition_max_ts[p.partition] = p.timestamp_ms
            if p.trace_id:
                trace_ids.add(p.trace_id)

        for partition, max_ts in partition_max_ts.items():
            await freshness.advance_partition_watermark(
                self._redis,
                partition_id=partition,
                batch_max_ts_ms=max_ts,
                now_ms=now_ms,
            )

        ttl_secs = settings.access_dedup_window_hours * 3600
        await dedupe.mark_seen(self._redis, [p.log_id for p in kept], ttl_seconds=ttl_secs)

        if settings.access_trace_seen_hint_enabled and trace_ids:
            await trace_hint.mark_traces(
                self._redis,
                trace_ids,
                ttl_seconds=ttl_secs,
            )

        return True

    async def _seek_to_committed(self) -> None:
        """CH 失败时 seek 回已提交 offset，让 _pending 消息重消费。"""
        if self._consumer is None:
            return
        for tp in self._consumer.assignment():
            committed = await self._consumer.committed(tp)
            if committed is not None:
                self._consumer.seek(tp, committed)

    async def _advance_idle_partitions(self) -> None:
        """空闲分区水位推进（lag=0 时防水位冻结，设计 §2.3）。"""
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
        """稳定采集时间优先级（设计 §2.3）：LogAppendTime > observed_timestamp > now。"""
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
