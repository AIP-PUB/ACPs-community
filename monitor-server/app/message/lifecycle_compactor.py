"""app/message/lifecycle_compactor.py — 生命周期派生周期任务（设计 §4.2，C-MESSAGE-MODEL-1）。

应用层周期任务：定时从 message_events 增量重算受影响 lifecycle_key 的完整生命周期行，
写入 message_lifecycle（ReplacingMergeTree(compacted_at)）。
access 无此组件（access 派生靠 CH MV 同步）。由 runtime 作为后台 task 启停。

两阶段增量重算：
  1. fetch_affected_lifecycle_keys(rebuild_from) → 受影响五元组
  2. recompute_lifecycles(key_tuples, compacted_at_ms) → INSERT INTO message_lifecycle
  3. 成功后推进 WM_LIFECYCLE 水位
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from app.core.config import settings
from app.message import freshness, planner, store
from app.message.exception import MessageCompactionError

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CompactionResult:
    """单轮压缩结果。"""

    affected: int
    written: int
    skipped: int
    watermark_ms: int | None


class LifecycleCompactor:
    """周期重算 lifecycle 派生表（设计 §6.14）。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def run(self) -> None:
        """周期循环（间隔 message_lifecycle_compact_interval_seconds）。

        异常不杀循环（记录后退避继续）。
        """
        while True:
            try:
                result = await self.run_once()
                logger.info(
                    "lifecycle_compactor: round complete",
                    affected=result.affected,
                    written=result.written,
                    skipped=result.skipped,
                    watermark_ms=result.watermark_ms,
                )
            except Exception:
                logger.error("lifecycle_compactor: unexpected error, backing off", exc_info=True)
            await asyncio.sleep(settings.message_lifecycle_compact_interval_seconds)

    async def run_once(self) -> CompactionResult:
        """执行单轮两阶段增量重算。"""
        wm = await freshness.read_compaction_watermark(self._redis, kind="lifecycle")
        rebuild_from = planner.compute_rebuild_from(
            last_watermark_ms=wm,
            overlap_seconds=settings.message_compaction_overlap_seconds,
        )

        key_tuples, max_observed = await store.fetch_affected_lifecycle_keys(rebuild_from)
        if not key_tuples:
            return CompactionResult(affected=0, written=0, skipped=0, watermark_ms=None)

        compacted_at_ms = int(datetime.now(UTC).timestamp() * 1000)

        try:
            written = await store.recompute_lifecycles(key_tuples, compacted_at_ms=compacted_at_ms)
        except MessageCompactionError:
            logger.error(
                "lifecycle_compactor: recompute failed, watermark not advanced",
                rebuild_from=rebuild_from,
                affected=len(key_tuples),
                exc_info=True,
            )
            return CompactionResult(affected=len(key_tuples), written=0, skipped=0, watermark_ms=None)

        if max_observed is not None:
            await freshness.set_compaction_watermark(self._redis, kind="lifecycle", watermark_ms=max_observed)

        return CompactionResult(
            affected=len(key_tuples),
            written=written,
            skipped=0,
            watermark_ms=max_observed,
        )
