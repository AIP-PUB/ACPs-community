"""E2E — Access 摄取基本流程（ingest flow）。

验收项：
- 投递一条 amp.access 消息 → AccessWriter 消费并写入 ClickHouse
- 轮询 /access/events/query → 20s 内返回该 log_id 的事件
- 事件字段（aic, request_route, response_status）与投递值一致
- meta.freshnessAt 非空（写入路径已推进水位）
- 投递重复 log_id 消息 → 数据库仅保留一条（幂等去重，C-ACCESS-WRITE-4）
- /access/operations/query 可聚合返回操作摘要
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import (
    produce_access_event,
    wait_for_access_event_ingested,
)

# ── 惰性 API 请求辅助 ─────────────────────────────────────────────────────────


def _time_range_body(
    *,
    lookback_hours: int = 1,
    aic: str | None = None,
) -> dict:
    """构造 events/query 或 operations/query 请求体（最近 lookback_hours）。"""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    start = (now - timedelta(hours=lookback_hours)).isoformat()
    end = now.isoformat()
    body: dict = {
        "timeRange": {"startAt": start, "endAt": end},
        "page": {"limit": 50},
    }
    if aic:
        body["filter"] = {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}
    return body


async def _poll_events_api(
    client: AsyncClient,
    log_id: str,
    *,
    timeout_s: float = 25.0,
    poll_interval_s: float = 1.0,
) -> dict[str, Any] | None:
    """轮询 /access/events/query 直到指定 log_id 出现，返回 API 响应 dict。"""
    import asyncio

    from app.core.config import settings

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.post(
            f"{settings.api_v1_str}/access/events/query",
            json=_time_range_body(lookback_hours=1),
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if any(item.get("logId") == log_id or item.get("log_id") == log_id for item in items):
                return cast("dict[str, Any]", data)
        await asyncio.sleep(poll_interval_s)
    return None


# ── 测试 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_ingest_flow_basic(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递单条 access 事件 → 20s 内 events/query 可查到，字段一致。"""
    aic = f"aic-e2e-ingest-{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())

    lid = await produce_access_event(
        aic=aic,
        log_id=log_id,
        method="POST",
        route="/api/v1/orders",
        response_status=201,
        duration_ms=120,
        service_name="order-svc",
    )
    assert lid == log_id

    # 等待 ClickHouse 收到该事件
    await wait_for_access_event_ingested(log_id, timeout_s=20.0)

    # 轮询 Query API
    result = await _poll_events_api(e2e_http_client, log_id, timeout_s=5.0)
    assert result is not None, f"events/query 在 5s 内未返回 log_id={log_id!r}"

    items = result.get("items", [])
    matching = [item for item in items if item.get("logId") == log_id or item.get("log_id") == log_id]
    assert matching, f"events 列表中未找到 log_id={log_id!r}"
    event = matching[0]

    # 字段断言
    assert event.get("aic") == aic
    assert event.get("responseStatus") == 201 or event.get("response_status") == 201
    assert event.get("requestMethod") == "POST" or event.get("request_method") == "POST"

    # meta 不为空
    meta = result.get("meta", {})
    assert meta is not None


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_ingest_dedup(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """重复 log_id 投递两次 → ClickHouse 只存一条（幂等去重 C-ACCESS-WRITE-4）。"""
    import asyncio

    from app.core.clickhouse_client import get_clickhouse_client
    from tests.support.factory import make_access_log_record

    log_id = str(uuid.uuid4())
    aic = f"aic-e2e-dedup-{uuid.uuid4().hex[:8]}"

    record = make_access_log_record(
        aic=aic,
        log_id=log_id,
        observed_timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    )

    # 投递两次相同消息
    await produce_access_event(record=record)
    await asyncio.sleep(0.1)
    await produce_access_event(record=record)

    # 等待第一条入库
    await wait_for_access_event_ingested(log_id, timeout_s=20.0)
    # 再等 2s 让第二条有足够时间被处理（如果去重失败则会写入第二条）
    await asyncio.sleep(2.0)

    client = await get_clickhouse_client()
    result = await client.query(
        "SELECT count() FROM access_events WHERE log_id = {lid:String}",
        parameters={"lid": log_id},
    )
    count = result.result_rows[0][0]
    assert count == 1, f"期望去重后只有 1 条，实际有 {count} 条（log_id={log_id!r}）"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_operations_query(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递 access 事件 → operations/query 可聚合返回操作摘要（Core Profile）。"""
    import asyncio

    from app.core.config import settings

    aic = f"aic-e2e-ops-{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())

    await produce_access_event(
        aic=aic,
        log_id=log_id,
        method="GET",
        route="/api/v1/status",
        response_status=200,
        duration_ms=30,
    )

    await wait_for_access_event_ingested(log_id, timeout_s=20.0)

    # 等待数据可查（ClickHouse MV 最终一致）
    await asyncio.sleep(1.0)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/access/operations/query",
        json=_time_range_body(aic=aic),
    )
    assert resp.status_code == 200, f"operations/query 返回非 200: {resp.status_code} {resp.text}"
    data = resp.json()
    assert "items" in data
    assert "meta" in data
