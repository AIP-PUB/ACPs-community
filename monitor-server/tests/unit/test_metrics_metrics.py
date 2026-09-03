"""tests/unit/test_metrics_metrics.py — MetricsMetrics + metrics_log_loop 单元测试（Step 8）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.metrics.metrics import MetricsMetrics, metrics_log_loop

# ── MetricsMetrics 基本功能 ───────────────────────────────────────────────────


def test_inc_increments_counter() -> None:
    m = MetricsMetrics()
    m.inc("foo")
    m.inc("foo")
    m.inc("foo", 3)
    assert m.snapshot()["foo"] == 5


def test_gauge_sets_value() -> None:
    m = MetricsMetrics()
    m.gauge("bar", 100)
    m.gauge("bar", 200)
    assert m.snapshot()["bar"] == 200


def test_observe_ms_accumulates_to_total() -> None:
    m = MetricsMetrics()
    m.observe_ms("latency", 100)
    m.observe_ms("latency", 50)
    assert m.snapshot()["latency_ms_total"] == 150


def test_snapshot_returns_copy() -> None:
    m = MetricsMetrics()
    m.inc("x", 1)
    snap1 = m.snapshot()
    m.inc("x", 1)
    snap2 = m.snapshot()
    assert snap1["x"] == 1
    assert snap2["x"] == 2


def test_snapshot_empty_by_default() -> None:
    m = MetricsMetrics()
    assert m.snapshot() == {}


def test_multiple_metrics_independent() -> None:
    m = MetricsMetrics()
    m.inc("a")
    m.gauge("b", 5)
    snap = m.snapshot()
    assert snap["a"] == 1
    assert snap["b"] == 5


# ── _sample_lag_gauge ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sample_lag_gauge_updates_gauge() -> None:
    """_sample_lag_gauge 从 freshness 读取 lag 并 gauge 更新。"""
    from app.metrics.freshness import FreshnessView
    from app.metrics.metrics import _sample_lag_gauge
    from app.metrics.metrics import metrics as global_metrics

    mock_view = FreshnessView(data_freshness_at_ms=1_700_000_000_000, ingestion_lag_ms=42_000, lagging=False)
    with (
        patch("app.core.redis_client.get_redis", return_value=AsyncMock()),
        patch("app.metrics.freshness.evaluate_freshness", new=AsyncMock(return_value=mock_view)),
    ):
        await _sample_lag_gauge()

    assert global_metrics.snapshot().get("amp_metrics_read_model_lag_ms", -1) == 42_000


@pytest.mark.asyncio
async def test_sample_lag_gauge_redis_exception_only_warns() -> None:
    """Redis 异常 → _sample_lag_gauge 只 WARNING，不传播异常。"""
    from app.metrics.metrics import _sample_lag_gauge

    with (
        patch("app.core.redis_client.get_redis", side_effect=RuntimeError("redis down")),
    ):
        # 不抛异常
        await _sample_lag_gauge()


# ── metrics_log_loop ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_log_loop_calls_sample_lag_and_snapshot() -> None:
    """metrics_log_loop 每轮先调 _sample_lag_gauge 再输出快照。"""
    import asyncio

    call_order: list[str] = []

    async def fake_sample_lag() -> None:
        call_order.append("sample_lag")

    async def fake_sleep(_: float) -> None:
        call_order.append("sleep")
        raise asyncio.CancelledError

    with (
        patch("app.metrics.metrics._sample_lag_gauge", new=fake_sample_lag),
        patch("app.metrics.metrics.asyncio.sleep", new=fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await metrics_log_loop(60)

    assert call_order[0] == "sample_lag"
    assert "sleep" in call_order
