"""demo-leader Metrics Emitter 单例与周期任务管理。

复用 amp_setup.LEADER_AIC（从 atr/acs.json 读取）。
指标写入 logs/amp_metrics.jsonl，由 Fluent Bit 转发到 Kafka amp.metrics。
发射间隔默认 30s，可由 AMP_METRICS_INTERVAL_SECONDS 覆盖。

resource 字段携带服务标识，与 Step E4 VictoriaMetrics 标签一致：
    service.name = "demo-leader"
    service.namespace = "acps-demo"
    deployment.environment.name = "dev"
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from acps_sdk.amp import MetricsEmitter
from acps_sdk.amp.metrics_demo import DemoMetricsSampler
from assistant.amp_setup import LEADER_AIC

_logger = logging.getLogger(__name__)

_METRICS_LOG_FILE = Path(__file__).parent.parent.parent / "logs" / "amp_metrics.jsonl"
_INTERVAL = float(os.environ.get("AMP_METRICS_INTERVAL_SECONDS", "30"))

_RESOURCE = {
    "service.name": "demo-leader",
    "service.namespace": "acps-demo",
    "deployment.environment.name": "dev",
}

_SAMPLER = DemoMetricsSampler(aic=LEADER_AIC)

LEADER_METRICS_EMITTER = MetricsEmitter(
    _METRICS_LOG_FILE,
    aic=LEADER_AIC,
    sampler=_SAMPLER,
    resource=_RESOURCE,
)

_metrics_task: asyncio.Task | None = None


def start_metrics -> None:
 """在已运行的事件循环中启动周期指标发射任务（幂等）。"""
    global _metrics_task
    if _metrics_task is None or _metrics_task.done:
        _metrics_task = asyncio.create_task(
            LEADER_METRICS_EMITTER.run_periodic(_INTERVAL),
            name="amp-metrics",
        )
        _logger.info("AMP metrics started (aic=%s, interval=%ss)", LEADER_AIC, _INTERVAL)


async def stop_metrics -> None:
 """取消周期指标发射任务。"""
    global _metrics_task
    if _metrics_task is not None:
        _metrics_task.cancel
        with contextlib.suppress(asyncio.CancelledError):
            await _metrics_task
        _metrics_task = None
