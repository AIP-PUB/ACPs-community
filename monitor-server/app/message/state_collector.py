"""app/message/state_collector.py — 目的地状态采集周期任务（设计 §3.2/§5.2）。

周期调用 DestinationStateSource.sample()，把样本写入 message_destination_state_snapshot，
并推进 state 水位。由 runtime 作为后台 task 启停。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from app.core.config import settings
from app.message import freshness, store
from app.message.destination_source import DestinationSample, DestinationStateSource

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)


def _sample_to_row(sample: DestinationSample) -> dict[str, object]:
    """DestinationSample → message_destination_state_snapshot 行字典。"""

    captured_dt = datetime.fromtimestamp(sample.captured_at_ms / 1000, tz=UTC)
    return {
        "captured_at": captured_dt,
        "system": sample.system,
        "destination_name": sample.destination_name,
        "destination_kind": sample.destination_kind,
        "virtual_host": sample.virtual_host,
        "visible_messages": sample.visible_messages,
        "inflight_messages": sample.inflight_messages,
        "delayed_messages": sample.delayed_messages,
        "dead_letter_messages": sample.dead_letter_messages,
        "oldest_message_age_seconds": sample.oldest_message_age_seconds,
        "active_consumers": sample.active_consumers,
        "size_bytes": sample.size_bytes,
    }


class DestinationStateCollector:
    """周期采集目的地状态 + 写快照 + 推水位（设计 §6.17）。"""

    def __init__(self, redis: Redis, source: DestinationStateSource) -> None:
        self._redis = redis
        self._source = source

    async def run(self) -> None:
        """周期循环（间隔 message_state_collect_interval_seconds）。

        异常不杀循环。
        """
        while True:
            try:
                count = await self.run_once()
                logger.info("state_collector: round complete", snapshot_count=count)
            except Exception:
                logger.error("state_collector: unexpected error, backing off", exc_info=True)
            await asyncio.sleep(settings.message_state_collect_interval_seconds)

    async def run_once(self) -> int:
        """执行单轮采集（§6.17）。

        空样本 → 不写、不推水位（保留旧快照，destinations 判窗口内有无）。
        返回写入行数。
        """
        samples = await self._source.sample()
        if not samples:
            return 0

        captured_at_ms = int(datetime.now(UTC).timestamp() * 1000)
        rows = [_sample_to_row(s) for s in samples]
        await store.insert_destination_snapshot(rows)
        await freshness.set_state_watermark(self._redis, captured_at_ms=captured_at_ms)
        return len(rows)
