"""app/message/throughput_compactor.py — 吞吐派生周期任务（设计 §4.4）。

与 lifecycle_compactor 同构，重算受影响 5min 桶的吞吐统计，
写入 message_destination_stats_5m（ReplacingMergeTree(compacted_at)）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from app.core.config import settings
from app.message import freshness, planner, store
from app.message.exception import MessageCompactionError
from app.message.lifecycle_compactor import CompactionResult

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)


class ThroughputCompactor:
    """周期重算 destination_stats_5m 派生表（设计 §6.15）。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def run(self) -> None:
        """周期循环（间隔 message_destination_stats_compact_interval_seconds）。"""
        while True:
            try:
                result = await self.run_once()
                logger.info(
                    "throughput_compactor: round complete",
                    affected=result.affected,
                    written=result.written,
                    watermark_ms=result.watermark_ms,
                )
            except Exception:
                logger.error("throughput_compactor: unexpected error, backing off", exc_info=True)
            await asyncio.sleep(settings.message_destination_stats_compact_interval_seconds)

    async def run_once(self) -> CompactionResult:
        """执行单轮两阶段增量重算。"""
        wm = await freshness.read_compaction_watermark(self._redis, kind="throughput")
        rebuild_from = planner.compute_rebuild_from(
            last_watermark_ms=wm,
            overlap_seconds=settings.message_compaction_overlap_seconds,
        )

        bucket_tuples, max_observed = await store.fetch_affected_buckets(rebuild_from)
        if not bucket_tuples:
            return CompactionResult(affected=0, written=0, skipped=0, watermark_ms=None)

        compacted_at_ms = int(datetime.now(UTC).timestamp() * 1000)

        try:
            written = await store.recompute_throughput_buckets(bucket_tuples, compacted_at_ms=compacted_at_ms)
        except MessageCompactionError:
            logger.error(
                "throughput_compactor: recompute failed, watermark not advanced",
                rebuild_from=rebuild_from,
                affected=len(bucket_tuples),
                exc_info=True,
            )
            return CompactionResult(affected=len(bucket_tuples), written=0, skipped=0, watermark_ms=None)

        if max_observed is not None:
            await freshness.set_compaction_watermark(self._redis, kind="throughput", watermark_ms=max_observed)

        return CompactionResult(
            affected=len(bucket_tuples),
            written=written,
            skipped=0,
            watermark_ms=max_observed,
        )
