"""app/system/maintenance.py — ISM 存量补挂调度 + 可选冷归档骨架（设计 §3.3 / §2.4）。

ISM Add Policy 实际调用在 store.ensure_ism_attached()（唯一 opensearch_client 调用点）；
本文件只调度 store 函数，不直接引用 opensearch_client。
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from app.system import store

logger = structlog.get_logger(__name__)


class SystemMaintenanceTask:
    """ISM 补挂周期任务（对齐 AccessArchiveTask 结构）。

    runtime 在 system_archive_enabled=True 时启动。
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """周期循环（system_archive_interval_seconds）；内部异常不杀循环。"""
        from app.core.config import settings

        interval = settings.system_archive_interval_seconds
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning("SystemMaintenanceTask: run_once failed (non-fatal)", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=interval,
                )

    async def run_once(self) -> None:
        """执行一次维护：ISM 补挂 + （可选）归档过期索引。"""
        await store.ensure_ism_attached()
        await archive_expiring_indices(before_days=0)

    def stop(self) -> None:
        self._stop_event.set()


async def archive_expiring_indices(*, before_days: int) -> int:
    """可选冷段归档骨架（默认 no-op）。

    将临近 archive_retention_days 的 cold 索引导出对象存储后再删。
    TODO：实现时若需要 OpenSearch API 调用，移入 store.py 并从此处调用。
    注：导出到对象存储、不在 OpenSearch 内的冷段不计入「有效保留窗口」(C-SYSTEM-RETENTION-1)。
    """
    return 0
