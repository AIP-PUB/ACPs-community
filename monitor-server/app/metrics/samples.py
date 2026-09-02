"""app/metrics/samples.py — MetricsBody 样本展开（纯函数）。

实现设计 §3.1 第 3 步、§4.1、§4.4：单条 MetricsBody → 多条 Remote Write 样本。
是写入正确性的核心纯函数（C-METRIC-WRITE-2/3）。

关键不变式：
- amp_window_request_total 是 Gauge 而非 Counter（C-METRIC-WRITE-3）。
- 所有 Sample.timestamp_ms == observed_at_ms（C-METRIC-WRITE-2）。
- 数值字段一律 float（偏异 D-2，int 字段也转 float 写入）。
- 全部 labels 展开后经 assert_label_cardinality_safe 防御性校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from acps_sdk.amp.models import MetricsBody

from app.metrics.labels import assert_label_cardinality_safe, base_labels, derive_resource_labels
from app.metrics.series import (
    AMP_LOAD_ACTIVE_TASKS,
    AMP_LOAD_CPU_USAGE,
    AMP_LOAD_DISK_USAGE,
    AMP_LOAD_MAX_ACTIVE_TASKS,
    AMP_LOAD_MAX_QUEUED_TASKS,
    AMP_LOAD_MEMORY_USAGE,
    AMP_LOAD_NETWORK_IN_USAGE,
    AMP_LOAD_NETWORK_OUT_USAGE,
    AMP_LOAD_QUEUED_TASKS,
    AMP_LOAD_UPTIME_SECONDS,
    AMP_SNAPSHOT_PRESENT,
    AMP_WINDOW_AVG_LATENCY_MS,
    AMP_WINDOW_AVG_THROUGHPUT_MBPS,
    AMP_WINDOW_LATENCY_MS,
    AMP_WINDOW_PEAK_THROUGHPUT_MBPS,
    AMP_WINDOW_REQUEST_PER_SECOND,
    AMP_WINDOW_REQUEST_TOTAL,
    AMP_WINDOW_SUCCESS_RATE,
)

# ── Sample 数据类 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sample:
    """单条 Remote Write 样本（§4.1）。"""

    metric_name: str
    """内部 amp_* series 名。"""

    labels: dict[str, str]
    """含 aic + resource 标签 + 语义标签（window/quantile 如适用）。"""

    value: float
    """样本值（一律 float，偏异 D-2）。"""

    timestamp_ms: int
    """全部样本共享同一 observed_at_ms（C-METRIC-WRITE-2）。"""


# ── LoadMetrics 字段 → series 名映射 ─────────────────────────────────────────

_LOAD_FIELD_TO_SERIES: Final[dict[str, str]] = {
    "active_tasks": AMP_LOAD_ACTIVE_TASKS,
    "queued_tasks": AMP_LOAD_QUEUED_TASKS,
    "max_active_tasks": AMP_LOAD_MAX_ACTIVE_TASKS,
    "max_queued_tasks": AMP_LOAD_MAX_QUEUED_TASKS,
    "cpu_usage": AMP_LOAD_CPU_USAGE,
    "memory_usage": AMP_LOAD_MEMORY_USAGE,
    "disk_usage": AMP_LOAD_DISK_USAGE,
    "network_in_usage": AMP_LOAD_NETWORK_IN_USAGE,
    "network_out_usage": AMP_LOAD_NETWORK_OUT_USAGE,
    "uptime_seconds": AMP_LOAD_UPTIME_SECONDS,
}
"""LoadMetrics 模型字段名（snake_case）→ 内部 series 名。"""

# ── WindowMetrics 非分位字段 → series 名映射 ──────────────────────────────────

_WINDOW_FIELD_TO_SERIES: Final[dict[str, str]] = {
    "success_rate": AMP_WINDOW_SUCCESS_RATE,
    "request_total": AMP_WINDOW_REQUEST_TOTAL,
    "request_per_second": AMP_WINDOW_REQUEST_PER_SECOND,
    "avg_throughput_mbps": AMP_WINDOW_AVG_THROUGHPUT_MBPS,
    "peak_throughput_mbps": AMP_WINDOW_PEAK_THROUGHPUT_MBPS,
    "avg_latency_ms": AMP_WINDOW_AVG_LATENCY_MS,
}
"""WindowMetrics 非分位字段名（snake_case）→ 内部 series 名。"""

# ── 分位字段 → quantile 标签值映射 ────────────────────────────────────────────

_QUANTILE_FIELD_TO_TAG: Final[dict[str, str]] = {
    "p50_latency_ms": "p50",
    "p75_latency_ms": "p75",
    "p80_latency_ms": "p80",
    "p90_latency_ms": "p90",
    "p95_latency_ms": "p95",
    "p99_latency_ms": "p99",
}
"""p*_latency_ms 字段名 → quantile 标签值（全部映射到 amp_window_latency_ms）。"""


def expand_metrics_body(
    *,
    aic: str,
    body: MetricsBody,
    resource: dict[str, Any] | None,
    observed_at_ms: int,
) -> list[Sample]:
    """将单条 MetricsBody 展开为 Remote Write 样本列表（§3.1 第 3 步、§4.1、§4.4）。

    Args:
        aic: Agent Identity Code。
        body: SDK MetricsBody 实例。
        resource: LogRecord.resource 字典（可为 None）。
        observed_at_ms: 稳定事件时间戳（毫秒），全部样本共享（C-METRIC-WRITE-2）。

    Returns:
        list[Sample]: 展开结果（至少包含 amp_snapshot_present 锚点样本）。
    """
    resource_lbs = derive_resource_labels(resource)
    base = base_labels(aic, resource_lbs)
    ts = observed_at_ms
    samples: list[Sample] = []

    def _add(metric_name: str, value: float | None, extra_labels: dict[str, str] | None = None) -> None:
        """内部工具：非 None 值才追加样本。"""
        if value is None:
            return
        lbs = {**base, **(extra_labels or {})}
        assert_label_cardinality_safe(lbs)
        samples.append(Sample(metric_name=metric_name, labels=lbs, value=float(value), timestamp_ms=ts))

    # 1. uptime_seconds（直接从 body 根字段读取）
    if body.uptime_seconds is not None:
        _add(AMP_LOAD_UPTIME_SECONDS, body.uptime_seconds)

    # 2. load_metrics 各字段
    if body.load_metrics is not None:
        lm = body.load_metrics
        for field_name, series_name in _LOAD_FIELD_TO_SERIES.items():
            if field_name == "uptime_seconds":
                # uptime_seconds 不在 load_metrics，已在步骤 1 处理
                continue
            val = getattr(lm, field_name, None)
            _add(series_name, val)

    # 3. window_metrics 各窗口
    if body.window_metrics:
        for wm in body.window_metrics:
            w = wm.window
            w_extra = {"window": w}
            # 非分位字段
            for field_name, series_name in _WINDOW_FIELD_TO_SERIES.items():
                val = getattr(wm, field_name, None)
                _add(series_name, val, w_extra)
            # 分位字段（p*_latency_ms → amp_window_latency_ms + quantile 标签）
            for field_name, quantile_tag in _QUANTILE_FIELD_TO_TAG.items():
                val = getattr(wm, field_name, None)
                _add(AMP_WINDOW_LATENCY_MS, val, {"window": w, "quantile": quantile_tag})

    # 4. 内部锚点 amp_snapshot_present = 1（§4.4：与全部样本共享同一时间戳）
    lbs_anchor = {**base}
    assert_label_cardinality_safe(lbs_anchor)
    samples.append(
        Sample(
            metric_name=AMP_SNAPSHOT_PRESENT,
            labels=lbs_anchor,
            value=1.0,
            timestamp_ms=ts,
        )
    )

    return samples


__all__ = [
    "Sample",
    "expand_metrics_body",
]
