"""E2E — Message 摄取基本流程与去重（H-1 场景 1）。

验收项：
- 投递一条 amp.message 消息 → MessageWriter 消费并写入 message_events
- 轮询 /message/events/query → 20s 内返回该 log_id 的事件
- meta.dataFreshnessAt 非空
- 投递重复 log_id 消息 → message_events 仅保留一条（C-MESSAGE-WRITE-2 去重）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


def _time_range_body(
    *,
    lookback_hours: int = 1,
    log_id: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "timeRange": {
            "startAt": (now - timedelta(hours=lookback_hours)).isoformat(),
            "endAt": now.isoformat(),
        },
        "page": {"limit": 50},
    }
    if log_id:
        body["filter"] = {"conditions": [{"field": "logId", "op": "eq", "value": log_id}], "logic": "and"}
    return body


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_message_events_ingest_basic(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """单条消息写入后 events/query 可命中。"""
    from app.core.config import settings

    log_id = str(uuid.uuid4())
    await produce_message_event(log_id=log_id, event_type="send")
    await wait_for_message_event_ingested(log_id, timeout_s=25)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/events/query",
        json=_time_range_body(log_id=log_id),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(item.get("logId") == log_id for item in data.get("items", []))
    assert data.get("meta", {}).get("dataFreshnessAt") is not None


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_message_events_deduplication(
    e2e_message_writer: dict[str, Any],
) -> None:
    """同 log_id 重复投递，message_events 仅保留一行（C-MESSAGE-WRITE-2）。"""
    from app.core.clickhouse_client import get_clickhouse_client
    from tests.support.factory import make_message_log_record

    log_id = str(uuid.uuid4())
    record = make_message_log_record(log_id=log_id, event_type="send")

    from tests.support.kafka_helper import produce_message_event as _produce

    for _ in range(2):
        await _produce(record=record)

    await wait_for_message_event_ingested(log_id, timeout_s=25)

    client = await get_clickhouse_client()
    result = await client.query(
        "SELECT count() FROM message_events WHERE log_id = {lid:String}",
        parameters={"lid": log_id},
    )
    count = result.result_rows[0][0]
    assert count == 1, f"期望 1 行，实际 {count} 行（去重失败）"
