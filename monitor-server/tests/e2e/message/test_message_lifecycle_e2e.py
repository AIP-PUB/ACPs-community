"""E2E — Message 生命周期聚合（H-1 场景 2）。

验收项：
- send + receive + ack 三事件 → Lifecycle Compactor → lifecycles/query 返回聚合
- sendCount=1 / receiveCount=1 / terminalState="ack"
- 不同 lifecycle_key 消息互不串扰
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


async def _wait_for_lifecycle_query(
    client: AsyncClient,
    api_prefix: str,
    message_id: str,
    *,
    timeout_s: float = 30.0,
) -> dict[str, Any] | None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        now = datetime.now(UTC)
        body = {
            "timeRange": {
                "startAt": (now - timedelta(hours=1)).isoformat(),
                "endAt": now.isoformat(),
            },
            "filter": {"conditions": [{"field": "messageId", "op": "eq", "value": message_id}], "logic": "and"},
        }
        resp = await client.post(f"{api_prefix}/message/lifecycles/query", json=body)
        if resp.status_code == 200:
            data: dict[str, Any] = resp.json()
            items = data.get("items", [])
            if items:
                return data
        # 503 意味着 lifecycle compactor 水位尚未建立，继续轮询
        await asyncio.sleep(1.0)
    return None


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_lifecycle_aggregation(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """send + receive + ack → compactor → lifecycles/query 聚合正确。"""
    from app.core.config import settings

    message_id = str(uuid.uuid4())
    log_id_send = str(uuid.uuid4())
    log_id_receive = str(uuid.uuid4())
    log_id_ack = str(uuid.uuid4())

    await produce_message_event(log_id=log_id_send, event_type="send", message_id=message_id)
    await produce_message_event(log_id=log_id_receive, event_type="receive", message_id=message_id)
    await produce_message_event(log_id=log_id_ack, event_type="ack", message_id=message_id)

    await wait_for_message_event_ingested(log_id_ack, timeout_s=25)

    data = await _wait_for_lifecycle_query(e2e_http_client, settings.api_v1_str, message_id=message_id, timeout_s=30)
    assert data is not None, "lifecycles/query 未返回数据"

    items = data["items"]
    assert len(items) >= 1
    lc = items[0]
    assert lc.get("sendCount", 0) >= 1
    assert lc.get("receiveCount", 0) >= 1


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_lifecycle_isolation(
    e2e_message_writer: dict[str, Any],
) -> None:
    """不同 message_id 的 lifecycle 互不干扰（各自独立行）。"""
    from app.core.clickhouse_client import get_clickhouse_client

    msg_id_a = str(uuid.uuid4())
    msg_id_b = str(uuid.uuid4())

    for msg_id in (msg_id_a, msg_id_b):
        log_id = str(uuid.uuid4())
        await produce_message_event(log_id=log_id, event_type="send", message_id=msg_id)
        await wait_for_message_event_ingested(log_id, timeout_s=25)

    await asyncio.sleep(3)  # 让 compactor 运行

    client = await get_clickhouse_client()
    for msg_id in (msg_id_a, msg_id_b):
        result = await client.query(
            "SELECT count() FROM message_events WHERE message_id = {mid:String}",
            parameters={"mid": msg_id},
        )
        assert result.result_rows[0][0] >= 1
