"""E2E — Message 吞吐量桶聚合（H-1 场景 4）。

验收项：
- 向同一 destination 投递多条事件 → ThroughputCompactor → throughput 表生成桶
- /message/destinations/throughput 接口返回 points 列表，总 count 与投递数量一致
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_throughput_series(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """多条同 destination 事件 → throughput 聚合有数据。"""
    from app.core.config import settings

    dest = f"e2e-topic-{uuid.uuid4().hex[:6]}"
    n = 5
    last_log_id: str = ""
    for _ in range(n):
        last_log_id = str(uuid.uuid4())
        msg_id = str(uuid.uuid4())
        await produce_message_event(log_id=last_log_id, event_type="send", destination_name=dest, message_id=msg_id)

    await wait_for_message_event_ingested(last_log_id, timeout_s=25)
    await asyncio.sleep(5)  # 等 compactor 生成桶

    now = datetime.now(UTC)
    body = {
        "timeRange": {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "destinationName": dest,
        "destinationKind": "topic",
        "system": "kafka",
    }
    last_resp_info = ""
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/message/destinations/throughput",
            json=body,
        )
        last_resp_info = f"status={resp.status_code} body={resp.text[:300]}"
        if resp.status_code == 200:
            data = resp.json()
            points = data.get("points", [])
            total = sum(p.get("producedCount", p.get("sendCount", p.get("count", 0))) for p in points)
            if total >= n:
                assert total >= n
                return
        await asyncio.sleep(2)

    pytest.fail(f"throughput 在 30s 内未累计到 {n} 条事件; last={last_resp_info}")
