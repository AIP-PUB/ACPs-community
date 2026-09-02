"""app/metrics/writer.py — MetricsWriter（BaseLogConsumer 子类）。

实现设计 §3.1、§10.1 写入路径：
  去重（Redis SET NX） → 展开 → 攒批 Remote Write → 成功后刷新缓存 + 推进水位。

关键偏异 D-1：覆写 run()，实现「5 秒或 10k 样本」攒批 Remote Write；
commit 延后到 flush 成功之后（C-METRIC-WRITE-1）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from time import monotonic
from typing import Any

import structlog
from acps_sdk.amp.models import LogRecord, MetricsBody, compute_log_id_fallback
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.kafka_consumer import BaseLogConsumer
from app.metrics import dedupe, snapshot_cache, tsdb
from app.metrics.exception import RemoteWriteError, UntimedMetricsError
from app.metrics.freshness import advance_watermark
from app.metrics.metrics import metrics as _metrics
from app.metrics.samples import Sample, expand_metrics_body
from app.metrics.snapshot_cache import CachedSnapshot

logger = structlog.get_logger(__name__)

_LOG_APPEND_TIME = 1


# ── 内部数据类 ─────────────────────────────────────────────────────────────────


@dataclass
class _PreparedRecord:
    """单条消息预处理结果（入攒批缓冲前的产物）。"""

    log_id: str
    aic: str
    observed_at_ms: int
    samples: list[Sample]
    cached_snapshot: CachedSnapshot


# ── MetricsWriter ─────────────────────────────────────────────────────────────


class MetricsWriter(BaseLogConsumer):
    """消费 amp.metrics，去重 → 展开 → 攒批 Remote Write → 刷新缓存 + 推进水位。

    不变式：_pending 始终对应"已消费但未提交 offset"的消息；
    commit 在 _flush_batch 成功后由 run() 调用（C-METRIC-WRITE-1）。
    """

    def __init__(self, redis: Redis) -> None:
        from app.core.config import get_settings

        s = get_settings()
        super().__init__(
            topic=s.metrics_topic,
            dlq_topic=s.metrics_dlq_topic,
            group_id=s.metrics_consumer_group,
            bootstrap_servers=s.kafka_bootstrap_servers,
            security_protocol=s.kafka_security_protocol,
            auto_offset_reset="earliest",
        )
        self._redis = redis
        self._pending: list[_PreparedRecord] = []
        self._last_flush_monotonic: float = 0.0

    # ── per-message 解析（不做 durable 写，只入攒批缓冲） ─────────────────────────

    async def handle_message(self, message: Any) -> None:
        """解析并展开单条消息，追加到攒批缓冲。

        不做 Remote Write，不提交 offset。由 run() 控制攒批与提交节奏。
        """
        if not message.value:
            return

        raw_dict: dict[str, Any] = json.loads(message.value)
        record = LogRecord.model_validate(raw_dict)

        # 类型过滤（只处理 metrics 类型）
        if record.log_type != "metrics":
            return

        log_id = record.log_id or compute_log_id_fallback(raw_dict)
        observed_at_ms = self._extract_observed_at(message, record)
        body = MetricsBody.model_validate(record.body or {})
        samples = expand_metrics_body(
            aic=record.aic,
            body=body,
            resource=record.resource,
            observed_at_ms=observed_at_ms,
        )
        cached_snapshot = self._build_cached_snapshot(record, body, observed_at_ms)

        self._pending.append(
            _PreparedRecord(
                log_id=log_id,
                aic=record.aic,
                observed_at_ms=observed_at_ms,
                samples=samples,
                cached_snapshot=cached_snapshot,
            )
        )

    # ── 异常分类：哪些不重试 ──────────────────────────────────────────────────────

    async def _process_with_retry(self, msg: Any) -> bool:
        """覆写：UntimedMetricsError / JSON / ValidationError → 不重试直达 DLQ。"""
        try:
            await self.handle_message(msg)
            return True
        except UntimedMetricsError, json.JSONDecodeError, ValidationError:
            logger.warning(
                "metrics_writer.message_skipped_to_dlq",
                exc_info=True,
                offset=getattr(msg, "offset", None),
            )
            return False
        except Exception:
            # 其余异常走基类指数退避（含 Pydantic 以外的运行时错误）
            return await super()._process_with_retry(msg)

    # ── 攒批主循环 ────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """覆写：「5 秒或 10k 样本」攒批 Remote Write，flush 成功后才提交 offset。

        偏异 D-1：基类逐条 commit 不满足「先攒批后写」的 C-METRIC-WRITE-1 语义。
        """
        if self._consumer is None:
            raise RuntimeError("Consumer 未启动，请先调用 start()")

        from app.core.config import get_settings

        s = get_settings()
        self._last_flush_monotonic = monotonic()

        while self._running:
            batches = await self._consumer.getmany(
                timeout_ms=s.metrics_writer_poll_timeout_ms,
                max_records=s.metrics_remote_write_batch_max_samples,
            )
            for _tp, msgs in batches.items():
                for msg in msgs:
                    if not await self._process_with_retry(msg):
                        await self._send_to_dlq(msg)

            pending_samples = sum(len(p.samples) for p in self._pending)
            elapsed_s = monotonic() - self._last_flush_monotonic
            due = elapsed_s >= s.metrics_remote_write_batch_interval_seconds

            if self._pending and (pending_samples >= s.metrics_remote_write_batch_max_samples or due):
                flushed = await self._flush_batch(list(self._pending))
                if flushed:
                    if self._consumer is not None:
                        await self._consumer.commit()
                else:
                    await self._seek_to_committed()
                self._pending.clear()
                self._last_flush_monotonic = monotonic()

    # ── 核心 flush 逻辑 ──────────────────────────────────────────────────────────

    async def _flush_batch(self, prepared: list[_PreparedRecord]) -> bool:
        """去重 → Remote Write → 刷缓存 + 推进水位。

        Returns:
            bool: True 表示成功（调用方 commit offset）；False 表示失败（调用方 seek 回退）。
        """
        if not prepared:
            return True

        log_ids = [p.log_id for p in prepared]
        claimed = await dedupe.claim_log_ids(self._redis, log_ids)
        kept = [p for p in prepared if p.log_id in claimed]

        if not kept:
            # 全是重复投递（含崩溃重投后的已写批次）
            _metrics.inc("amp_metrics_dedupe_skipped_total", len(prepared))
            return True

        all_samples = [s for p in kept for s in p.samples]

        try:
            await tsdb.remote_write(all_samples)
        except RemoteWriteError:
            # 回滚去重占用（确保重消费可再占）
            await dedupe.release_log_ids(self._redis, [p.log_id for p in kept])
            _metrics.inc("amp_metrics_remote_write_failures_total")
            logger.error("metrics_writer.remote_write_failed", sample_count=len(all_samples), exc_info=True)
            return False

        # Remote Write 成功后刷新 Redis 快照缓存（失败只 WARNING）
        for p in kept:
            try:
                await snapshot_cache.upsert_snapshot(self._redis, p.cached_snapshot)
            except Exception:
                logger.warning("metrics_writer.snapshot_upsert_failed", aic=p.aic, exc_info=True)
                _metrics.inc("amp_metrics_snapshot_cache_write_failures_total")

        # 推进 dataFreshnessAt 水位
        max_observed = max(p.observed_at_ms for p in kept)
        await advance_watermark(self._redis, max_observed)

        _metrics.inc("amp_metrics_writer_accepted_total", len(kept))
        _metrics.inc("amp_metrics_writer_samples_total", len(all_samples))
        _metrics.inc("amp_metrics_dedupe_skipped_total", len(prepared) - len(kept))

        logger.debug(
            "metrics_writer.flush_ok",
            accepted=len(kept),
            skipped=len(prepared) - len(kept),
            samples=len(all_samples),
        )
        return True

    # ── 工具方法 ──────────────────────────────────────────────────────────────────

    async def _seek_to_committed(self) -> None:
        """Remote Write 持续失败时回退到已提交 offset（去重保幂等）。"""
        if self._consumer is None:
            return
        partitions = self._consumer.assignment()
        committed = await self._consumer.committed(partitions)
        for tp in partitions:
            offset_meta = committed.get(tp)
            if offset_meta is not None:
                self._consumer.seek(tp, offset_meta.offset)
            else:
                await self._consumer.seek_to_beginning(tp)
        logger.warning("metrics_writer.seeked_to_committed")

    def _extract_observed_at(self, message: Any, record: LogRecord) -> int:
        """提取稳定 observedAt（§2.3 时间优先级：LogAppendTime > observed_timestamp）。

        - message.timestamp（Kafka LogAppendTime，broker 赋予，message_timestamp_type=LogAppendTime）优先
        - 退而求其次：record.observed_timestamp（ISO 8601）
        - 均不可用 → raise UntimedMetricsError（不重试，直达 DLQ）

        Returns:
            int: observedAt 毫秒时间戳。

        Raises:
            UntimedMetricsError: 无可用稳定时间戳。
        """
        from datetime import datetime

        # Kafka 消息时间戳（毫秒，broker LogAppendTime，timestamp_type==1）
        ts = getattr(message, "timestamp", None)
        if getattr(message, "timestamp_type", None) == _LOG_APPEND_TIME and ts is not None and ts > 0:
            return int(ts)

        # 回退：observed_timestamp（ISO 8601）
        if record.observed_timestamp:
            try:
                dt = datetime.fromisoformat(record.observed_timestamp)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return int(dt.timestamp() * 1000)
            except ValueError:
                pass

        raise UntimedMetricsError(f"No stable observedAt available for log_id={record.log_id}, aic={record.aic}")

    def _build_cached_snapshot(self, record: LogRecord, body: MetricsBody, observed_at_ms: int) -> CachedSnapshot:
        """从 LogRecord + MetricsBody 预构造 CachedSnapshot。"""
        from app.metrics.labels import derive_resource_labels

        resource_lbs = derive_resource_labels(record.resource)
        return CachedSnapshot(
            aic=record.aic,
            observed_at_ms=observed_at_ms,
            uptime_seconds=body.uptime_seconds,
            load_metrics=body.load_metrics,
            window_metrics=body.window_metrics,
            service_name=resource_lbs.get("service_name"),
            service_namespace=resource_lbs.get("service_namespace"),
            deployment_env=resource_lbs.get("deployment_env"),
        )


__all__ = ["MetricsWriter"]
