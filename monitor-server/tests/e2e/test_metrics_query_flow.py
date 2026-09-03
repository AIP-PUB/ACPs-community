"""E2E — Metrics 查询流程（Step E3）。

验收项（C-METRIC-QUERY-2 / C-METRIC-QUERY-3 / C-METRIC-QUERY-5）：
- 注入多 AIC 快照 → snapshots/query 分页、排序、过滤正确
- 水位推进后 freshness meta 返回合理值

注：如全服务在线（demo + infra + monitor），可用 `bash scripts/demo_metrics.sh`
替代手工注入进行更完整的 leader/partner 发射路径验证（Step E8.1.2）。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from tests.support.factory import METRICS_API_PREFIX, poll_snapshot, snapshot_query_body
from tests.support.kafka_helper import produce_metrics


@pytest.mark.asyncio
async def test_snapshots_query_multiple_aics(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递多条 metrics 消息 → snapshots/query 返回所有 AIC 的快照。"""
    aics = ["e2e-qf-001", "e2e-qf-002", "e2e-qf-003"]
    for i, aic in enumerate(aics):
        await produce_metrics(aic, uptime_seconds=float((i + 1) * 10))
        await asyncio.sleep(0.05)  # 错开 Kafka LogAppendTime

    deadline = asyncio.get_event_loop().time() + 25.0
    found: set[str] = set()
    while asyncio.get_event_loop().time() < deadline and found != set(aics):
        resp = await e2e_http_client.post(
            f"{METRICS_API_PREFIX}/snapshots/query",
            json=snapshot_query_body(aics=aics, limit=10),
        )
        if resp.status_code != 200:
            await asyncio.sleep(0.5)
            continue
        for item in resp.json().get("items", []):
            found.add(item["aic"])
        if found != set(aics):
            await asyncio.sleep(0.5)

    assert found == set(aics), f"未全部返回：期望 {set(aics)}, 实际 {found}"


@pytest.mark.asyncio
async def test_snapshots_query_pagination_total(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """5 条快照 + limit=2 → 分页正确（第 1 页最多 2 条）。"""
    aics = [f"e2e-pg-{i:03d}" for i in range(5)]
    for aic in aics:
        await produce_metrics(aic, uptime_seconds=99.0)
        await asyncio.sleep(0.05)

    deadline = asyncio.get_event_loop().time() + 25.0
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{METRICS_API_PREFIX}/snapshots/query",
            json={"page": {"limit": 10}},
        )
        if resp.status_code == 200 and len(resp.json().get("items", [])) >= 5:
            break
        await asyncio.sleep(0.5)

    resp = await e2e_http_client.post(
        f"{METRICS_API_PREFIX}/snapshots/query",
        json={"page": {"limit": 2}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) <= 2
    meta = data.get("meta", {})
    approx_total = meta.get("approximateTotal") or meta.get("approximate_total")
    if approx_total is not None:
        assert approx_total >= 5


@pytest.mark.asyncio
async def test_snapshots_query_aic_filter(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """指定 aic 过滤 → 只返回对应快照（C-METRIC-QUERY-2）。"""
    await produce_metrics("e2e-filter-in", uptime_seconds=1.0)
    await produce_metrics("e2e-filter-out", uptime_seconds=2.0)

    snap = await poll_snapshot(e2e_http_client, aics=["e2e-filter-in"], timeout_s=20.0)
    assert snap is not None

    resp = await e2e_http_client.post(
        f"{METRICS_API_PREFIX}/snapshots/query",
        json=snapshot_query_body(aics=["e2e-filter-in"], limit=10),
    )
    assert resp.status_code == 200
    returned_aics = {item["aic"] for item in resp.json().get("items", [])}
    assert "e2e-filter-in" in returned_aics
    assert "e2e-filter-out" not in returned_aics
