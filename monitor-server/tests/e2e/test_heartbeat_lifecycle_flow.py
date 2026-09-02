"""E2E — 心跳生命周期流程（Step 11）。

验收项（C-TIME-2）：
- 停止心跳 → 等待静默阈值（testing.toml: 2s）→ reconciler 静默扫描
- /liveness/{aic} 变为 silent（livenessState=silent）

耗时说明：
  testing.toml: silence_threshold=2s, silent_scan_interval=1s
  测试内显式 sleep(5s) = 2s 阈值 + 1s 扫描 + 2s 缓冲
  总耗时约 6-8 秒（本地 Redpanda），属于正常 E2E 测试范围。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from app.core.config import settings
from tests.support.kafka_helper import produce_heartbeat

_HB = f"{settings.api_v1_str}/heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_lifecycle_silent(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """心跳停止后 → 等待静默阈值 → liveness 变为 silent（C-TIME-2 验收）。

    流程：
    1. 投递一次心跳 → 轮询至 isAlive=True
    2. 停止投递心跳，等待 5s（silence_threshold 2s + scan_interval 1s + 2s 缓冲）
    3. 轮询至 livenessState=silent
    """

    aic = "e2e-aic-lifecycle-001"

    # Step 1：发送一次心跳，等待 alive
    await produce_heartbeat(aic)
    deadline = asyncio.get_event_loop().time() + 10.0
    alive = False
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.get(f"{_HB}/liveness/{aic}")
        if resp.status_code == 200 and resp.json()["data"].get("isAlive") is True:
            alive = True
            break
        await asyncio.sleep(0.3)
    assert alive, f"{aic} 未在 10s 内变为 alive"

    # Step 2：不再发送心跳，等待 reconciler 静默扫描（testing 环境 silence_threshold=2s）
    # 等待至少 silence_threshold + scan_interval + 缓冲 = 2+2+1 = 5s
    await asyncio.sleep(5.0)

    # Step 3：轮询 liveness 变为 silent
    deadline = asyncio.get_event_loop().time() + 10.0
    silent = False
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.get(f"{_HB}/liveness/{aic}")
        if resp.status_code == 200:
            data = resp.json()["data"]
            if data.get("livenessState") == "silent":
                silent = True
                break
        await asyncio.sleep(0.5)

    assert silent, f"{aic} 未在 10s 内变为 silent（livenessState）"
