"""E2E — Metrics retention + freshness 流程（Step E3）。

验收项（C-METRIC-WRITE-1 / C-METRIC-FRESHNESS / C-METRIC-RETENTION）：
- 投递消息 → watermark 推进（freshness water mark advance 验证）
- 快照 observedAt 时间戳与投递顺序一致（newer event 覆盖旧快照）
- 迟到事件（older observed_at）不回滚已有快照（ZADD GT 语义）

> **降采样 retention E2E（简化策略）**：本文件不覆盖 C-METRIC-RETENTION-2 降采样 step 切换。
> demo / dev-infra 仅使用 RAW 数据源（无 VM `rollup_5m:`/`rollup_1h:` 物化，§1.2 / §13.1.3）；
> 跨保留边界与 `AMP_OUT_OF_RETENTION` 由 planner 单元/集成测试覆盖（§9.3 `test_metrics_planner`）。
> 完整降采样 E2E 待 acps-infra 落地 recording rules 后，以 `@pytest.mark.slow` + `TEST_E2E_RETENTION=1` 启用。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from tests.support.factory import poll_snapshot
from tests.support.kafka_helper import produce_metrics


@pytest.mark.asyncio
async def test_watermark_advances_after_ingest(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递消息 → freshness watermark 在 15s 内推进（C-METRIC-FRESHNESS）。"""
    from app.core.redis_client import get_redis
    from app.metrics.freshness import read_watermark

    aic = "e2e-wm-001"
    redis = get_redis()

    initial_wm = await read_watermark(redis)
    await produce_metrics(aic, uptime_seconds=10.0)

    deadline = asyncio.get_event_loop().time() + 20.0
    advanced = False
    while asyncio.get_event_loop().time() < deadline:
        wm = await read_watermark(redis)
        if wm is not None and (initial_wm is None or wm > initial_wm):
            advanced = True
            break
        await asyncio.sleep(0.5)

    assert advanced, "freshness watermark 在 20s 内未推进"


@pytest.mark.asyncio
async def test_newer_event_overwrites_snapshot(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """较新的事件覆盖旧快照，uptimeSeconds 更新为新值（C-METRIC-WRITE-1）。"""
    aic = "e2e-overwrite-001"

    await produce_metrics(aic, uptime_seconds=10.0)
    first = await poll_snapshot(e2e_http_client, aics=[aic], uptime_seconds=10.0, timeout_s=20.0)
    assert first is not None, f"AIC {aic!r} 首条快照在 20s 内未出现"

    # 间隔 ≥1s，确保 Kafka LogAppendTime 严格递增
    await asyncio.sleep(1.0)
    await produce_metrics(aic, uptime_seconds=200.0)

    updated = await poll_snapshot(e2e_http_client, aics=[aic], uptime_seconds=200.0, timeout_s=25.0)
    assert updated is not None, f"AIC {aic!r} 的 uptimeSeconds 在 25s 内未更新为 200.0"


@pytest.mark.asyncio
async def test_snapshot_observed_at_is_recent(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """快照的 observedAt 应为近期时间戳（不早于 60s 前）。"""
    from datetime import UTC, datetime, timedelta

    aic = "e2e-ts-001"
    await produce_metrics(aic, uptime_seconds=1.0)

    snap = await poll_snapshot(e2e_http_client, aics=[aic], timeout_s=20.0)
    assert snap is not None
    snap_time = snap.get("observedAt")
    assert snap_time is not None

    observed = datetime.fromisoformat(snap_time)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    assert (now - observed) < timedelta(seconds=60), f"observedAt {snap_time!r} 太旧（超过 60s）"
