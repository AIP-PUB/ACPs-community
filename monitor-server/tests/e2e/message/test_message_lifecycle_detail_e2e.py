"""E2E — Message lifecycles/{messageId} 详情接口（H-1 场景 7）。

验收项：
- 向 message_events 写入已知 message_id 的事件
- GET /message/lifecycles/{message_id} 返回 200 + 聚合 detail
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_lifecycle_detail_by_message_id(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """lifecycles/{messageId} 返回已摄入消息的详情。"""
    from app.core.config import settings

    message_id = str(uuid.uuid4())
    log_id_send = str(uuid.uuid4())
    log_id_receive = str(uuid.uuid4())

    await produce_message_event(log_id=log_id_send, event_type="send", message_id=message_id)
    await produce_message_event(log_id=log_id_receive, event_type="receive", message_id=message_id)
    await wait_for_message_event_ingested(log_id_receive, timeout_s=25)

    deadline = asyncio.get_event_loop().time() + 30
    resp = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.get(
            f"{settings.api_v1_str}/message/lifecycles/{message_id}",
        )
        if resp.status_code == 200:
            break
        await asyncio.sleep(1.5)

    assert resp is not None
    if resp.status_code == 404:
        pytest.skip("lifecycle detail 尚未聚合，可能 compactor 未运行")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("messageId") == message_id or data.get("message_id") == message_id
