"""app/message/archive.py — 冷数据归档/保留辅助（设计 §6.21）。

结构对齐 access.archive：
  - `MessageArchiveTask` 类（__init__(redis) / run() / run_once() / stop()）
  - 模块级纯函数 `raw_archive_is_trustworthy`（被 §6.14/§6.15 完整性保护调用）

首版实现保留类骨架，归档逻辑 no-op（message_archive_enabled 默认 False）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)


def raw_archive_is_trustworthy(
    *,
    min_event_ts_ms: int,
    raw_retention_ms: int,
    now_ms: int,
) -> bool:
    """判断给定 lifecycle_key / bucket 的最早事件是否在热保留窗内（C-MESSAGE-RETENTION-5）。

    纯函数，无副作用，无 I/O。
    返回 True ⟺ min_event_ts_ms >= now_ms - raw_retention_ms（事件在热窗内，可信）。
    compactor 在受影响 key 事件超窗且不可信时跳过重算，保留旧派生行。
    """
    earliest_retained_ms = now_ms - raw_retention_ms
    return min_event_ts_ms >= earliest_retained_ms


class MessageArchiveTask:
    """Message 归档后台任务（对齐 AccessArchiveTask，首版 no-op 骨架）。

    runtime 在 message_archive_enabled=True 时实例化并启动；默认禁用。
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._running = False

    async def run(self) -> None:
        """周期归档循环；异常不杀循环（设计 §6.21）。"""
        from app.core.config import settings

        self._running = True
        import asyncio

        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("message archive run_once failed")
            await asyncio.sleep(getattr(settings, "message_archive_interval_seconds", 3600))

    async def run_once(self) -> int:
        """执行一轮归档检查；首版 no-op，返回处理数 0。"""
        return 0

    def stop(self) -> None:
        """停止归档循环（幂等）。"""
        self._running = False
