"""app/metrics/metrics.py — Metrics 模块进程内指标注册表（设计 §6.17）。

复用 Heartbeat 的 HeartbeatMetrics 同款轻量内存注册表模式
（inc / gauge / observe_ms / snapshot）。
指标名严格采用设计 §9.1 清单，供 metrics_log_loop 周期输出及测试断言。
Prometheus / OTLP 导出延后至 D-4 阶段（偏异 D-4），届时直接对齐本注册表键名。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import structlog

logger = structlog.get_logger(__name__)


class MetricsMetrics:
    """Metrics 模块进程内指标注册表（内存 counter / gauge / ms 累计）。"""

    def __init__(self) -> None:
        self._data: dict[str, int] = defaultdict(int)

    def inc(self, name: str, value: int = 1) -> None:
        """递增计数器。

        Args:
            name: 指标名（§9.1 清单）。
            value: 增量（默认 1）。
        """
        self._data[name] += value

    def gauge(self, name: str, value: int) -> None:
        """设置 gauge（覆盖上次值）。

        Args:
            name: 指标名。
            value: 当前值。
        """
        self._data[name] = value

    def observe_ms(self, name: str, ms: int) -> None:
        """累加耗时 ms 到 '<name>_ms_total'。

        Args:
            name: 耗时指标前缀。
            ms: 本次观测 ms 数值。
        """
        self._data[f"{name}_ms_total"] += ms

    def snapshot(self) -> dict[str, int]:
        """返回所有指标当前值的快照 dict（独立 copy）。"""
        return dict(self._data)


metrics = MetricsMetrics()
"""模块级单例（writer / service / snapshot_service 均持此引用）。"""


# ── gauge 采样 ─────────────────────────────────────────────────────────────────


async def _sample_lag_gauge() -> None:
    """读取 dataFreshnessAt 水位，刷新 amp_metrics_read_model_lag_ms（§6.17）。

    Redis / 水位异常时只 WARNING，不中断 metrics_log_loop。
    """
    from app.core.redis_client import get_redis
    from app.metrics.freshness import evaluate_freshness

    try:
        redis = get_redis()
        view = await evaluate_freshness(redis)
        metrics.gauge("amp_metrics_read_model_lag_ms", view.ingestion_lag_ms or 0)
    except Exception:
        logger.warning("metrics._sample_lag_gauge.failed", exc_info=True)


# ── 周期输出循环 ──────────────────────────────────────────────────────────────


async def metrics_log_loop(interval_s: int) -> None:
    """周期 INFO 输出全量指标快照（structlog kv 风格，同 heartbeat）。

    每轮先 _sample_lag_gauge() 刷新 amp_metrics_read_model_lag_ms，
    再输出完整快照。

    Args:
        interval_s: 输出周期（秒，来自配置 metrics_metrics_log_interval_seconds）。
    """
    while True:
        try:
            await _sample_lag_gauge()
            snap = metrics.snapshot()
            if snap:
                logger.info("metrics.periodic_snapshot", **snap)
        except Exception:
            logger.warning("metrics_log_loop.iteration_failed", exc_info=True)
        await asyncio.sleep(interval_s)


__all__ = [
    "MetricsMetrics",
    "metrics",
    "metrics_log_loop",
]
