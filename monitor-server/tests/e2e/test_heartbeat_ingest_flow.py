"""E2E — 心跳 ingest 基本流程（Step 11）。

验收项：
- 投递一条心跳消息 → 轮询 /liveness/{aic} 至 alive（最多 10s）
- 返回新鲜度字段 isAlive=True、lastSeenAt 非空
- /summary 返回 aliveCount >= 1
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from app.core.config import settings
from tests.support.kafka_helper import produce_heartbeat

_HB = f"{settings.api_v1_str}/heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_ingest_flow(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递心跳 → liveness 变为 alive → summary.aliveCount >= 1。"""
    aic = "e2e-aic-ingest-001"

    # 投递心跳
    await produce_heartbeat(aic)

    # 轮询 /liveness/{aic} 最多 10s
    deadline = asyncio.get_event_loop().time() + 10.0
    alive = False
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.get(f"{_HB}/liveness/{aic}")
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            body = resp.json()
            if body["data"].get("isAlive") is True:
                alive = True
                break
        await asyncio.sleep(0.4)

    assert alive, f"AIC {aic!r} 在 10s 内未变为 alive"

    # 验证元数据字段
    resp = await e2e_http_client.get(f"{_HB}/liveness/{aic}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["isAlive"] is True
    assert data["livenessState"] == "alive"
    assert data["lastSeenAt"], "lastSeenAt 不能为空"
    assert data["silenceDurationSeconds"] >= 0

    # /summary aliveCount >= 1
    resp = await e2e_http_client.get(f"{_HB}/summary")
    assert resp.status_code == 200
    assert resp.json()["data"]["aliveCount"] >= 1
