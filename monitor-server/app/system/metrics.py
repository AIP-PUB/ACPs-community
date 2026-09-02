"""app/system/metrics.py — 进程内指标注册（对齐 access/message）。

指标键名对齐设计 §9.1。
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import structlog

logger = structlog.get_logger(__name__)


class SystemMetrics:
    """轻量进程内指标（counters + histograms + gauges）。

    线程不安全（asyncio 单线程模型保证足够）。
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list[float]] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, by: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + by

    def observe(self, name: str, value: float) -> None:
        self._histograms.setdefault(name, []).append(value)

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def snapshot(self) -> dict[str, Any]:
        """返回当前全部指标快照（用于周期日志输出）。"""
        snap: dict[str, Any] = {}
        snap.update(self._counters)
        for name, vals in self._histograms.items():
            if vals:
                snap[f"{name}_count"] = len(vals)
                snap[f"{name}_sum_ms"] = sum(vals)
                snap[f"{name}_avg_ms"] = sum(vals) / len(vals)
        snap.update(self._gauges)
        return snap


# 进程级单例（与 message/access metrics 同型）
metrics: Final = SystemMetrics()

# ── 指标键名（设计 §9.1） ─────────────────────────────────────────────────────

# 写入
AMP_SYSTEM_WRITER_ACCEPTED_TOTAL = "amp_system_writer_accepted_total"
AMP_SYSTEM_WRITER_NORMALIZED_TOTAL = "amp_system_writer_normalized_total"
AMP_SYSTEM_BULK_INDEX_LATENCY_MS = "amp_system_bulk_index_latency_ms"
AMP_SYSTEM_BULK_INDEX_FAILURES_TOTAL = "amp_system_bulk_index_failures_total"

# 新鲜度
AMP_SYSTEM_READ_MODEL_LAG_MS = "amp_system_read_model_lag_ms"

# 查询
AMP_SYSTEM_EVENTS_QUERY_LATENCY_MS = "amp_system_events_query_latency_ms"

# 运维诊断
AMP_SYSTEM_WRITER_DLQ_TOTAL = "amp_system_writer_dlq_total"
AMP_SYSTEM_PIT_OPEN_TOTAL = "amp_system_pit_open_total"
AMP_SYSTEM_PIT_EXPIRED_TOTAL = "amp_system_pit_expired_total"


async def metrics_log_loop(interval_seconds: int) -> None:
    """周期输出指标快照到结构化日志（对齐 message/access 的 metrics_log_loop）。"""
    while True:
        await asyncio.sleep(interval_seconds)
        snap = metrics.snapshot()
        logger.info("system.metrics", **snap)
