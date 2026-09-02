"""AMP Metrics 公共指标名目录（spec §5.3 字段名 / 设计 §4.1.1 对外列）。

这是发射端、查询客户端与 Provider 三方共享的语义契约：
- 合法的公共 metric 取值（camelCase）
- 所属指标族（MetricFamily）
- 是否必须携带 window 参数
- 是否隐含固定 quantile

全部条目由 spec §5.3 LoadMetrics / WindowMetrics 字段 alias 与 MetricsBody 字段派生，
入 SDK 防止三方漂移（C-METRIC-QUERY-6）。
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from pydantic import BaseModel


# ── 指标族（设计 §6.0.1.E；时间聚合兼容矩阵以此为键） ──────────────────────────

class MetricFamily(str, Enum):
    """AMP 指标族。"""

    SAMPLE_COUNT_GAUGE = "sample_count_gauge"
    """采样计数 Gauge（activeTasks / queuedTasks / max* 等瞬时计数）。"""

    RESOURCE_USAGE_GAUGE = "resource_usage_gauge"
    """资源利用率 Gauge（cpu/memory/disk/network 使用率 0-100）。"""

    WINDOW_RATE_LATENCY = "window_rate_latency"
    """窗口 summary 速率 / 时延类（successRate / *LatencyMs / requestPerSecond 等）。"""

    WINDOW_TOTAL = "window_total"
    """窗口 summary 总量类（requestTotal）。"""

    MONOTONIC_UPTIME_GAUGE = "monotonic_uptime_gauge"
    """单调递增运行时长 Gauge（uptimeSeconds）。"""


# ── 已知窗口与分位枚举（仅作校验/文档参考，取值由上报数据决定） ────────────────────

KNOWN_WINDOWS: Final = ("PT1M", "PT5M", "PT15M", "PT1H")
"""AMP spec §5.3 WindowMetrics 标准 window 枚举（ISO 8601 Duration）。"""

KNOWN_QUANTILES: Final = ("p50", "p75", "p80", "p90", "p95", "p99")
"""AMP Metrics 标准分位名（字符串，与 TSDB 标签值一致）。"""


# ── 单个公共 metric 的语义属性 ─────────────────────────────────────────────────

class PublicMetricMeta(BaseModel):
    """单个公共 metric 的对外语义属性（目录条目）。"""

    public_name: str
    """camelCase 业务名，如 "cpuUsage" / "p95LatencyMs"。"""

    family: MetricFamily
    """指标族，决定合法聚合算子（avg / sum / max / last 等）。"""

    needs_window: bool
    """是否必须由 filter.window / rankings.window 指定。窗口型指标为 True。"""

    fixed_quantile: str | None
    """p*LatencyMs 隐含固定 quantile 字符串（"p50"…"p99"），其余为 None。"""


# ── §4.1.1 公共指标目录（22 条目，不含内部 amp_* series 名） ─────────────────────

PUBLIC_METRIC_CATALOG: Final[dict[str, PublicMetricMeta]] = {
    # ── LoadMetrics：采样计数 Gauge ──────────────────────────────────────────
    "activeTasks": PublicMetricMeta(
        public_name="activeTasks",
        family=MetricFamily.SAMPLE_COUNT_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "queuedTasks": PublicMetricMeta(
        public_name="queuedTasks",
        family=MetricFamily.SAMPLE_COUNT_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "maxActiveTasks": PublicMetricMeta(
        public_name="maxActiveTasks",
        family=MetricFamily.SAMPLE_COUNT_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "maxQueuedTasks": PublicMetricMeta(
        public_name="maxQueuedTasks",
        family=MetricFamily.SAMPLE_COUNT_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    # ── LoadMetrics：资源利用率 Gauge ────────────────────────────────────────
    "cpuUsage": PublicMetricMeta(
        public_name="cpuUsage",
        family=MetricFamily.RESOURCE_USAGE_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "memoryUsage": PublicMetricMeta(
        public_name="memoryUsage",
        family=MetricFamily.RESOURCE_USAGE_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "diskUsage": PublicMetricMeta(
        public_name="diskUsage",
        family=MetricFamily.RESOURCE_USAGE_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "networkInUsage": PublicMetricMeta(
        public_name="networkInUsage",
        family=MetricFamily.RESOURCE_USAGE_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    "networkOutUsage": PublicMetricMeta(
        public_name="networkOutUsage",
        family=MetricFamily.RESOURCE_USAGE_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
    # ── WindowMetrics：速率 / 时延类（无固定 quantile）──────────────────────
    "successRate": PublicMetricMeta(
        public_name="successRate",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile=None,
    ),
    "requestPerSecond": PublicMetricMeta(
        public_name="requestPerSecond",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile=None,
    ),
    "avgThroughputMBps": PublicMetricMeta(
        public_name="avgThroughputMBps",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile=None,
    ),
    "peakThroughputMBps": PublicMetricMeta(
        public_name="peakThroughputMBps",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile=None,
    ),
    "avgLatencyMs": PublicMetricMeta(
        public_name="avgLatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile=None,
    ),
    # ── WindowMetrics：总量类 ────────────────────────────────────────────────
    "requestTotal": PublicMetricMeta(
        public_name="requestTotal",
        family=MetricFamily.WINDOW_TOTAL,
        needs_window=True,
        fixed_quantile=None,
    ),
    # ── WindowMetrics：带固定 quantile 的时延 ────────────────────────────────
    "p50LatencyMs": PublicMetricMeta(
        public_name="p50LatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile="p50",
    ),
    "p75LatencyMs": PublicMetricMeta(
        public_name="p75LatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile="p75",
    ),
    "p80LatencyMs": PublicMetricMeta(
        public_name="p80LatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile="p80",
    ),
    "p90LatencyMs": PublicMetricMeta(
        public_name="p90LatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile="p90",
    ),
    "p95LatencyMs": PublicMetricMeta(
        public_name="p95LatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile="p95",
    ),
    "p99LatencyMs": PublicMetricMeta(
        public_name="p99LatencyMs",
        family=MetricFamily.WINDOW_RATE_LATENCY,
        needs_window=True,
        fixed_quantile="p99",
    ),
    # ── MetricsBody：单调运行时长 Gauge ─────────────────────────────────────
    "uptimeSeconds": PublicMetricMeta(
        public_name="uptimeSeconds",
        family=MetricFamily.MONOTONIC_UPTIME_GAUGE,
        needs_window=False,
        fixed_quantile=None,
    ),
}


def lookup_public_metric(name: str) -> PublicMetricMeta | None:
    """按公共名查询 metric 元信息。

    Args:
        name: 公共 camelCase 业务名（如 "cpuUsage"、"p95LatencyMs"）。

    Returns:
        PublicMetricMeta 若命中；None 若未命中（包括内部 amp_* 名，C-METRIC-QUERY-6）。
    """
    return PUBLIC_METRIC_CATALOG.get(name)


__all__ = [
    "MetricFamily",
    "KNOWN_WINDOWS",
    "KNOWN_QUANTILES",
    "PublicMetricMeta",
    "PUBLIC_METRIC_CATALOG",
    "lookup_public_metric",
]
