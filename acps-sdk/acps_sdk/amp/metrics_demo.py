"""AMP Metrics 合成采样器（demo/联调专用）。

.. warning::
    本模块**仅供 demo 与本地联调使用**，请勿用于生产环境。
    生产环境应由 Agent 业务层实现 SampleProvider 协议，测量真实负载数据。

DemoMetricsSampler 以 aic 为种子做随机游走（确保不同 aic 取值各异、同 aic 跨次调用值连续），
合成以下字段：
- uptime_seconds：单调递增（按调用次数自增）
- load_metrics：activeTasks / queuedTasks / cpuUsage / memoryUsage
- window_metrics：多窗口（PT1M / PT5M / PT15M）× successRate / RPS / requestTotal / p50/p95/p99 latency

合成规则：
- p50 <= p95 <= p99（分位顺序不变）
- successRate 主要落 [90, 100]（偶有小毛刺）
- active/queued 为非负整数，不超过 max 上限
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from acps_sdk.amp.models import LoadMetrics, MetricsBody, WindowMetrics

_WINDOWS = ["PT1M", "PT5M", "PT15M"]

# 各 aic 的状态缓存（游走基点）
_state: dict[str, dict[str, float]] = {}


def _seed_float(aic: str, key: str) -> float:
    """用 aic + key 生成 [0, 1) 的确定性伪随机数。"""
    h = hashlib.sha256(f"{aic}:{key}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _walk(state: dict[str, float], key: str, *, lo: float, hi: float, step: float) -> float:
    """在 [lo, hi] 内对 state[key] 做随机游走，step 为最大单次变化量。

    首次调用以 key 的确定性伪随机数初始化。
    """
    if key not in state:
        seed = _seed_float("init", key)
        state[key] = lo + seed * (hi - lo)

    import random

    rng = random.Random(f"{state[key]:.6f}:{key}")
    delta = rng.uniform(-step, step)
    state[key] = max(lo, min(hi, state[key] + delta))
    return state[key]


class DemoMetricsSampler:
    """指标合成采样器（demo/联调专用）。

    Args:
        aic: 发射方 Agent Instance Context 标识符（用于派生随机种子）。
        max_active: 最大 activeTasks 上限（默认 10）。
        max_queued: 最大 queuedTasks 上限（默认 20）。
    """

    def __init__(self, aic: str, max_active: int = 10, max_queued: int = 20) -> None:
        self._aic = aic
        self._max_active = max_active
        self._max_queued = max_queued
        self._start_time = time.monotonic()
        self._call_count = 0

    def sample(self) -> MetricsBody:
        """生成一份合成 MetricsBody。"""
        self._call_count += 1
        state = _state.setdefault(self._aic, {})

        uptime = round(time.monotonic() - self._start_time, 3)

        # LoadMetrics
        active = int(round(_walk(state, f"{self._aic}:active", lo=0, hi=self._max_active, step=2)))
        queued = int(round(_walk(state, f"{self._aic}:queued", lo=0, hi=self._max_queued, step=3)))
        cpu = round(_walk(state, f"{self._aic}:cpu", lo=0.0, hi=100.0, step=5.0), 1)
        mem = round(_walk(state, f"{self._aic}:mem", lo=10.0, hi=90.0, step=3.0), 1)
        load = LoadMetrics(
            active_tasks=active,
            queued_tasks=queued,
            max_active_tasks=self._max_active,
            max_queued_tasks=self._max_queued,
            cpu_usage=cpu,
            memory_usage=mem,
        )

        # WindowMetrics
        windows: list[WindowMetrics] = []
        for win in _WINDOWS:
            sr = round(_walk(state, f"{self._aic}:{win}:sr", lo=85.0, hi=100.0, step=2.0), 2)
            rps = round(_walk(state, f"{self._aic}:{win}:rps", lo=0.1, hi=50.0, step=3.0), 2)
            total = int(round(rps * _window_seconds(win)))
            p50 = round(_walk(state, f"{self._aic}:{win}:p50", lo=5.0, hi=100.0, step=5.0), 1)
            # Ensure p50 <= p95 <= p99 by building on each previous
            p95 = round(max(p50, _walk(state, f"{self._aic}:{win}:p95", lo=p50, hi=p50 * 3.0, step=10.0)), 1)
            p99 = round(max(p95, _walk(state, f"{self._aic}:{win}:p99", lo=p95, hi=p95 * 2.0, step=15.0)), 1)
            windows.append(
                WindowMetrics(
                    window=win,
                    success_rate=sr,
                    request_total=total,
                    request_per_second=rps,
                    p50_latency_ms=p50,
                    p95_latency_ms=p95,
                    p99_latency_ms=p99,
                )
            )

        return MetricsBody(
            uptime_seconds=uptime,
            load_metrics=load,
            window_metrics=windows,
        )


def _window_seconds(iso_duration: str) -> float:
    """将 ISO 8601 Duration 转换为秒（仅支持 PTxM/PTxH 简单格式）。"""
    if iso_duration.startswith("PT") and iso_duration.endswith("M"):
        return float(iso_duration[2:-1]) * 60.0
    if iso_duration.startswith("PT") and iso_duration.endswith("H"):
        return float(iso_duration[2:-1]) * 3600.0
    if iso_duration.startswith("PT") and iso_duration.endswith("S"):
        return float(iso_duration[2:-1])
    return 300.0  # fallback: 5 min


__all__ = ["DemoMetricsSampler"]
