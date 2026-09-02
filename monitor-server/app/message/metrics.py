"""app/message/metrics.py — Message 模块进程内指标注册表（设计 §6.22 / §9.1）。

对齐 access.metrics 轻量进程内模式：模块级单例 `metrics` + `metrics_log_loop`。
键名逐一对齐源设计 §9.1，禁止自创新名。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Final

import structlog

logger = structlog.get_logger(__name__)


class MessageMetrics:
    """Message 模块进程内指标注册表（内存 counter / gauge / ms 累计）。

    接口与 AccessMetrics 完全一致（inc / observe / set_gauge / snapshot）。
    """

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


metrics: Final = MessageMetrics()


async def metrics_log_loop(interval_seconds: int) -> None:
    """周期把 MessageMetrics 快照打成一条 structlog INFO（设计 §6.22）。"""
    while True:
        await asyncio.sleep(interval_seconds)
        snap = metrics.snapshot()
        if snap:
            logger.info("message.metrics", **snap)
