"""app/access/metrics.py — Access 模块进程内指标注册表（设计 §6.19）。

与 Metrics / Heartbeat 同款轻量内存注册表模式。
OTLP 导出延后至 E 阶段，届时直接对齐本注册表键名。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Final

import structlog

logger = structlog.get_logger(__name__)


class AccessMetrics:
    """Access 模块进程内指标注册表（内存 counter / gauge / ms 累计）。"""

    def __init__(self) -> None:
        self._data: dict[str, int | float] = defaultdict(int)

    def inc(self, name: str, by: int = 1) -> None:
        self._data[name] = int(self._data[name]) + by

    def observe(self, name: str, value_ms: float) -> None:
        self._data[name] = value_ms

    def set_gauge(self, name: str, value: float) -> None:
        self._data[name] = value

    def snapshot(self) -> dict[str, int | float]:
        return dict(self._data)


metrics: Final = AccessMetrics()


async def metrics_log_loop(interval_seconds: int) -> None:
    """周期把 AccessMetrics 快照打成一条 structlog INFO（设计 §6.19）。"""
    while True:
        await asyncio.sleep(interval_seconds)
        snap = metrics.snapshot()
        if snap:
            logger.info("access.metrics", **snap)
