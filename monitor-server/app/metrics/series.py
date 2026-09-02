"""app/metrics/series.py — 内部 series 命名、public→internal 映射与数据源解析（纯函数）。

public 名 → 内部 amp_* series 名的映射只此一处（C-METRIC-QUERY-6 的代码层落点）。
内部名不对外暴露：lookup_public_metric("amp_load_cpu_usage") 必须返回 None（SDK 层保证）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from acps_sdk.amp.metrics_catalog import PublicMetricMeta, lookup_public_metric

from app.metrics.exception import MetricUnsupportedError

# ── 内部 series 名常量（§4.1，不对外暴露至 public API） ─────────────────────────

# LoadMetrics
AMP_LOAD_ACTIVE_TASKS: Final = "amp_load_active_tasks"
AMP_LOAD_QUEUED_TASKS: Final = "amp_load_queued_tasks"
AMP_LOAD_MAX_ACTIVE_TASKS: Final = "amp_load_max_active_tasks"
AMP_LOAD_MAX_QUEUED_TASKS: Final = "amp_load_max_queued_tasks"
AMP_LOAD_CPU_USAGE: Final = "amp_load_cpu_usage"
AMP_LOAD_MEMORY_USAGE: Final = "amp_load_memory_usage"
AMP_LOAD_DISK_USAGE: Final = "amp_load_disk_usage"
AMP_LOAD_NETWORK_IN_USAGE: Final = "amp_load_network_in_usage"
AMP_LOAD_NETWORK_OUT_USAGE: Final = "amp_load_network_out_usage"
AMP_LOAD_UPTIME_SECONDS: Final = "amp_load_uptime_seconds"

# WindowMetrics（无固定 quantile）
AMP_WINDOW_SUCCESS_RATE: Final = "amp_window_success_rate"
AMP_WINDOW_REQUEST_TOTAL: Final = "amp_window_request_total"
AMP_WINDOW_REQUEST_PER_SECOND: Final = "amp_window_request_per_second"
AMP_WINDOW_AVG_THROUGHPUT_MBPS: Final = "amp_window_avg_throughput_mbps"
AMP_WINDOW_PEAK_THROUGHPUT_MBPS: Final = "amp_window_peak_throughput_mbps"
AMP_WINDOW_AVG_LATENCY_MS: Final = "amp_window_avg_latency_ms"

# WindowMetrics（带 quantile 标签）
AMP_WINDOW_LATENCY_MS: Final = "amp_window_latency_ms"

# 内部辅助序列（§4.4）——快照锚点，不经 public API 返回
AMP_SNAPSHOT_PRESENT: Final = "amp_snapshot_present"

# ── public camelCase 名 → 内部 series 名（§4.1.1 内部列） ────────────────────

_PUBLIC_TO_SERIES: Final[dict[str, str]] = {
    "activeTasks": AMP_LOAD_ACTIVE_TASKS,
    "queuedTasks": AMP_LOAD_QUEUED_TASKS,
    "maxActiveTasks": AMP_LOAD_MAX_ACTIVE_TASKS,
    "maxQueuedTasks": AMP_LOAD_MAX_QUEUED_TASKS,
    "cpuUsage": AMP_LOAD_CPU_USAGE,
    "memoryUsage": AMP_LOAD_MEMORY_USAGE,
    "diskUsage": AMP_LOAD_DISK_USAGE,
    "networkInUsage": AMP_LOAD_NETWORK_IN_USAGE,
    "networkOutUsage": AMP_LOAD_NETWORK_OUT_USAGE,
    "uptimeSeconds": AMP_LOAD_UPTIME_SECONDS,
    "successRate": AMP_WINDOW_SUCCESS_RATE,
    "requestTotal": AMP_WINDOW_REQUEST_TOTAL,
    "requestPerSecond": AMP_WINDOW_REQUEST_PER_SECOND,
    "avgThroughputMBps": AMP_WINDOW_AVG_THROUGHPUT_MBPS,
    "peakThroughputMBps": AMP_WINDOW_PEAK_THROUGHPUT_MBPS,
    "avgLatencyMs": AMP_WINDOW_AVG_LATENCY_MS,
    # p*LatencyMs 全部映射到同一带 quantile 标签的 series（§4.1.1）
    "p50LatencyMs": AMP_WINDOW_LATENCY_MS,
    "p75LatencyMs": AMP_WINDOW_LATENCY_MS,
    "p80LatencyMs": AMP_WINDOW_LATENCY_MS,
    "p90LatencyMs": AMP_WINDOW_LATENCY_MS,
    "p95LatencyMs": AMP_WINDOW_LATENCY_MS,
    "p99LatencyMs": AMP_WINDOW_LATENCY_MS,
}


# ── ResolvedMetric ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedMetric:
    """public 名解析结果：同时携带响应回显名、内部 series 名与指标元信息。"""

    public_name: str
    """响应回显用（C-METRIC-QUERY-6：响应中一律回显公共名）。"""

    series_name: str
    """内部 amp_* 名（查询 TSDB 时使用）。"""

    meta: PublicMetricMeta
    """指标族 / needs_window / fixed_quantile。"""


def resolve_public_metric(public_name: str) -> ResolvedMetric:
    """将 public camelCase 名解析为 ResolvedMetric。

    Args:
        public_name: 公共业务名（如 "cpuUsage"、"p95LatencyMs"）。

    Returns:
        ResolvedMetric

    Raises:
        MetricUnsupportedError: 名字不在目录中（422）。内部 amp_* 名也无法通过此函数（C-METRIC-QUERY-6）。
    """
    meta = lookup_public_metric(public_name)
    if meta is None:
        raise MetricUnsupportedError(public_name)
    series_name = _PUBLIC_TO_SERIES[public_name]
    return ResolvedMetric(public_name=public_name, series_name=series_name, meta=meta)


# ── QuerySource ───────────────────────────────────────────────────────────────


class QuerySource(StrEnum):
    """TSDB 数据源类型（§6.0.1.C）。"""

    RAW = "RAW"
    """原始序列，min_step 15s。"""

    DS_5M = "DS_5M"
    """5 分钟降采样，min_step 5m。"""

    DS_1H = "DS_1H"
    """1 小时降采样，min_step 1h。"""


class MetricSourceResolver:
    """把内部 series 名映射到某查询源上的物理 series 名（§6.0.1.C）。

    避免在查询代码里硬编码 rollup 命名前缀；未来 VM 改命名只改此处。
    """

    def resolve(self, series_name: str, source: QuerySource) -> str:
        """返回在指定数据源上对应的物理 series 名。

        Args:
            series_name: 内部 amp_* 名。
            source: 查询源类型。

        Returns:
            str: 物理 series 名（如 "rollup_5m:amp_load_cpu_usage"）。
        """
        if source == QuerySource.RAW:
            return series_name
        if source == QuerySource.DS_5M:
            return f"rollup_5m:{series_name}"
        # DS_1H
        return f"rollup_1h:{series_name}"


__all__ = [
    "AMP_LOAD_ACTIVE_TASKS",
    "AMP_LOAD_CPU_USAGE",
    "AMP_LOAD_DISK_USAGE",
    "AMP_LOAD_MAX_ACTIVE_TASKS",
    "AMP_LOAD_MAX_QUEUED_TASKS",
    "AMP_LOAD_MEMORY_USAGE",
    "AMP_LOAD_NETWORK_IN_USAGE",
    "AMP_LOAD_NETWORK_OUT_USAGE",
    "AMP_LOAD_QUEUED_TASKS",
    "AMP_LOAD_UPTIME_SECONDS",
    "AMP_SNAPSHOT_PRESENT",
    "AMP_WINDOW_AVG_LATENCY_MS",
    "AMP_WINDOW_AVG_THROUGHPUT_MBPS",
    "AMP_WINDOW_LATENCY_MS",
    "AMP_WINDOW_PEAK_THROUGHPUT_MBPS",
    "AMP_WINDOW_REQUEST_PER_SECOND",
    "AMP_WINDOW_REQUEST_TOTAL",
    "AMP_WINDOW_SUCCESS_RATE",
    "MetricSourceResolver",
    "QuerySource",
    "ResolvedMetric",
    "resolve_public_metric",
]
