"""tests/amp/test_amp_metrics_demo.py — DemoMetricsSampler 单元测试（Step E1）。"""

from __future__ import annotations

import pytest

from acps_sdk.amp.metrics_demo import DemoMetricsSampler
from acps_sdk.amp.models import MetricsBody


@pytest.fixture
def sampler() -> DemoMetricsSampler:
    return DemoMetricsSampler(aic="test-aic-demo-001")


# ── sample 基本正确性 ─────────────────────────────────────────────────────────


def test_sample_returns_metrics_body(sampler: DemoMetricsSampler) -> None:
    """sample() 返回合法 MetricsBody 实例。"""
    result = sampler.sample()
    assert isinstance(result, MetricsBody)


def test_sample_uptime_nonnegative(sampler: DemoMetricsSampler) -> None:
    """uptime_seconds >= 0。"""
    result = sampler.sample()
    assert result.uptime_seconds is not None
    assert result.uptime_seconds >= 0.0


def test_sample_load_metrics_present(sampler: DemoMetricsSampler) -> None:
    """load_metrics 不为 None。"""
    result = sampler.sample()
    assert result.load_metrics is not None


def test_sample_window_metrics_present(sampler: DemoMetricsSampler) -> None:
    """window_metrics 非空列表（至少 1 个窗口）。"""
    result = sampler.sample()
    assert result.window_metrics is not None
    assert len(result.window_metrics) >= 1


# ── 分位顺序约束 ──────────────────────────────────────────────────────────────


def test_sample_quantile_order(sampler: DemoMetricsSampler) -> None:
    """p50 <= p95 <= p99（每个窗口）。"""
    for _ in range(10):
        result = sampler.sample()
        assert result.window_metrics is not None
        for w in result.window_metrics:
            if w.p50_latency_ms is not None and w.p95_latency_ms is not None:
                assert w.p50_latency_ms <= w.p95_latency_ms, f"p50={w.p50_latency_ms} > p95={w.p95_latency_ms}"
            if w.p95_latency_ms is not None and w.p99_latency_ms is not None:
                assert w.p95_latency_ms <= w.p99_latency_ms, f"p95={w.p95_latency_ms} > p99={w.p99_latency_ms}"


# ── successRate 范围 ──────────────────────────────────────────────────────────


def test_sample_success_rate_in_range(sampler: DemoMetricsSampler) -> None:
    """successRate ∈ [0, 100] (每个窗口)。"""
    for _ in range(20):
        result = sampler.sample()
        assert result.window_metrics is not None
        for w in result.window_metrics:
            assert 0.0 <= w.success_rate <= 100.0


# ── active/queued 范围 ────────────────────────────────────────────────────────


def test_sample_load_counts_nonnegative(sampler: DemoMetricsSampler) -> None:
    """activeTasks / queuedTasks >= 0。"""
    for _ in range(10):
        result = sampler.sample()
        assert result.load_metrics is not None
        assert result.load_metrics.active_tasks >= 0
        assert result.load_metrics.queued_tasks >= 0


# ── 同 aic 跨次连续（游走有界）────────────────────────────────────────────────


def test_sample_continuity_same_aic(sampler: DemoMetricsSampler) -> None:
    """同 aic 的连续 50 次采样，cpu_usage 保持有界（0-100）。"""
    for _ in range(50):
        result = sampler.sample()
        assert result.load_metrics is not None
        if result.load_metrics.cpu_usage is not None:
            assert 0.0 <= result.load_metrics.cpu_usage <= 100.0


# ── 不同 aic 取值各异 ─────────────────────────────────────────────────────────


def test_sample_different_aics_differ() -> None:
    """不同 aic 在首次采样时 cpu_usage / successRate 不完全相同（种子各异）。"""
    results = {}
    for aic in ["aic-A", "aic-B", "aic-C", "aic-D", "aic-E"]:
        s = DemoMetricsSampler(aic=aic)
        body = s.sample()
        assert body.load_metrics is not None
        results[aic] = body.load_metrics.cpu_usage

    # 至少 2 个 aic 取值不同
    unique = len(set(v for v in results.values() if v is not None))
    assert unique >= 2, f"所有 aic 的 cpu_usage 相同，种子可能未生效: {results}"
