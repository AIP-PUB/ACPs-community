"""tests/amp/test_metrics_catalog.py — 公共 metric 名目录单元测试（Step 1 TDD）。

覆盖目标（设计 §4.4）：
  - 目录键集合 == spec §5.3 公共名集合（防字段漂移）
  - p*LatencyMs 的 fixed_quantile 为字符串前缀（非浮点）
  - 窗口型 needs_window == True；负载/资源/uptime 类 == False
  - 内部名不命中（C-METRIC-QUERY-6）
"""

from __future__ import annotations

import pytest

from acps_sdk.amp.metrics_catalog import (
    PUBLIC_METRIC_CATALOG,
    MetricFamily,
    PublicMetricMeta,
    lookup_public_metric,
)


# ── 1. 目录键完整性（与 spec §5.3 字段别名集合一致）────────────────────────────

_EXPECTED_NAMES = frozenset(
    {
        # LoadMetrics
        "activeTasks",
        "queuedTasks",
        "maxActiveTasks",
        "maxQueuedTasks",
        "cpuUsage",
        "memoryUsage",
        "diskUsage",
        "networkInUsage",
        "networkOutUsage",
        # WindowMetrics（无固定 quantile）
        "successRate",
        "requestTotal",
        "requestPerSecond",
        "avgThroughputMBps",
        "peakThroughputMBps",
        "avgLatencyMs",
        # WindowMetrics（固定 quantile）
        "p50LatencyMs",
        "p75LatencyMs",
        "p80LatencyMs",
        "p90LatencyMs",
        "p95LatencyMs",
        "p99LatencyMs",
        # MetricsBody
        "uptimeSeconds",
    }
)


def test_catalog_key_set_matches_spec() -> None:
    """目录键集合与 spec §5.3 公共名集合完全一致，不多不少。"""
    assert set(PUBLIC_METRIC_CATALOG.keys()) == _EXPECTED_NAMES


def test_catalog_has_22_entries() -> None:
    """目录共 22 条目（LoadMetrics 9 + WindowMetrics 12 + uptimeSeconds 1）。"""
    assert len(PUBLIC_METRIC_CATALOG) == 22


# ── 2. p*LatencyMs 分位约束 ──────────────────────────────────────────────────

_LATENCY_QUANTILE_MAP = {
    "p50LatencyMs": "p50",
    "p75LatencyMs": "p75",
    "p80LatencyMs": "p80",
    "p90LatencyMs": "p90",
    "p95LatencyMs": "p95",
    "p99LatencyMs": "p99",
}


@pytest.mark.parametrize("name,expected_quantile", _LATENCY_QUANTILE_MAP.items())
def test_latency_fixed_quantile_is_string(name: str, expected_quantile: str) -> None:
    """p*LatencyMs 的 fixed_quantile 为字符串前缀（非浮点 0.5 等）。"""
    meta = PUBLIC_METRIC_CATALOG[name]
    assert meta.fixed_quantile == expected_quantile, f"{name}: expected '{expected_quantile}', got {meta.fixed_quantile!r}"
    assert isinstance(meta.fixed_quantile, str)


@pytest.mark.parametrize("name,expected_quantile", _LATENCY_QUANTILE_MAP.items())
def test_latency_family_and_needs_window(name: str, expected_quantile: str) -> None:
    """p*LatencyMs 属 WINDOW_RATE_LATENCY 族，且 needs_window=True。"""
    meta = PUBLIC_METRIC_CATALOG[name]
    assert meta.family == MetricFamily.WINDOW_RATE_LATENCY
    assert meta.needs_window is True


# ── 3. requestTotal 族别 ─────────────────────────────────────────────────────

def test_request_total_family_is_window_total() -> None:
    """requestTotal 属 WINDOW_TOTAL（非 WINDOW_RATE_LATENCY）。"""
    meta = PUBLIC_METRIC_CATALOG["requestTotal"]
    assert meta.family == MetricFamily.WINDOW_TOTAL
    assert meta.needs_window is True
    assert meta.fixed_quantile is None


# ── 4. uptimeSeconds 约束 ─────────────────────────────────────────────────────

def test_uptime_seconds_meta() -> None:
    """uptimeSeconds: needs_window=False, family=MONOTONIC_UPTIME_GAUGE。"""
    meta = PUBLIC_METRIC_CATALOG["uptimeSeconds"]
    assert meta.needs_window is False
    assert meta.family == MetricFamily.MONOTONIC_UPTIME_GAUGE
    assert meta.fixed_quantile is None


# ── 5. 窗口型 vs 非窗口型 ────────────────────────────────────────────────────

_WINDOW_METRICS = {
    "successRate",
    "requestTotal",
    "requestPerSecond",
    "avgThroughputMBps",
    "peakThroughputMBps",
    "avgLatencyMs",
    "p50LatencyMs",
    "p75LatencyMs",
    "p80LatencyMs",
    "p90LatencyMs",
    "p95LatencyMs",
    "p99LatencyMs",
}

_NON_WINDOW_METRICS = {
    "activeTasks",
    "queuedTasks",
    "maxActiveTasks",
    "maxQueuedTasks",
    "cpuUsage",
    "memoryUsage",
    "diskUsage",
    "networkInUsage",
    "networkOutUsage",
    "uptimeSeconds",
}


@pytest.mark.parametrize("name", sorted(_WINDOW_METRICS))
def test_window_metrics_need_window(name: str) -> None:
    """窗口型指标 needs_window == True。"""
    assert PUBLIC_METRIC_CATALOG[name].needs_window is True


@pytest.mark.parametrize("name", sorted(_NON_WINDOW_METRICS))
def test_non_window_metrics_no_window(name: str) -> None:
    """负载/资源/uptime 类 needs_window == False。"""
    assert PUBLIC_METRIC_CATALOG[name].needs_window is False


# ── 6. lookup_public_metric 函数 ─────────────────────────────────────────────

def test_lookup_public_metric_hit() -> None:
    """lookup_public_metric('cpuUsage') 命中，返回正确 meta。"""
    meta = lookup_public_metric("cpuUsage")
    assert meta is not None
    assert isinstance(meta, PublicMetricMeta)
    assert meta.public_name == "cpuUsage"
    assert meta.family == MetricFamily.RESOURCE_USAGE_GAUGE


def test_lookup_public_metric_miss_internal_name() -> None:
    """内部 amp_* 名 lookup 返回 None（C-METRIC-QUERY-6）。"""
    assert lookup_public_metric("amp_load_cpu_usage") is None
    assert lookup_public_metric("amp_window_success_rate") is None


def test_lookup_public_metric_miss_nonexistent() -> None:
    """不存在的名返回 None。"""
    assert lookup_public_metric("nonexistent") is None
    assert lookup_public_metric("") is None


# ── 7. 所有 LoadMetrics SAMPLE_COUNT_GAUGE 无 fixed_quantile ─────────────────

@pytest.mark.parametrize("name", ["activeTasks", "queuedTasks", "maxActiveTasks", "maxQueuedTasks"])
def test_sample_count_gauge_no_fixed_quantile(name: str) -> None:
    """SAMPLE_COUNT_GAUGE 指标 fixed_quantile 为 None。"""
    meta = PUBLIC_METRIC_CATALOG[name]
    assert meta.family == MetricFamily.SAMPLE_COUNT_GAUGE
    assert meta.fixed_quantile is None


# ── 8. 所有 RESOURCE_USAGE_GAUGE 无 fixed_quantile ──────────────────────────

@pytest.mark.parametrize("name", ["cpuUsage", "memoryUsage", "diskUsage", "networkInUsage", "networkOutUsage"])
def test_resource_usage_gauge_no_fixed_quantile(name: str) -> None:
    """RESOURCE_USAGE_GAUGE 指标 fixed_quantile 为 None。"""
    meta = PUBLIC_METRIC_CATALOG[name]
    assert meta.family == MetricFamily.RESOURCE_USAGE_GAUGE
    assert meta.fixed_quantile is None
