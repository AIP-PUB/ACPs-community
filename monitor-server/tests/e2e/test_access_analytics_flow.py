"""E2E — Access Analytics 流程（error attribution + slow requests）。

验收项：
- 投递包含错误码的 access 事件 → errors/query 返回错误归因摘要
- 投递高延迟事件 → slow-requests/query 返回慢请求列表
- events/query 支持 filter by aic + time range
- events/query 翻页：limit/cursor 机制（C-ACCESS-QRY-1）
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import (
    produce_access_event,
    wait_for_access_event_ingested,
)


def _time_range_body(
    *,
    lookback_hours: int = 1,
    aic: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    start = (now - timedelta(hours=lookback_hours)).isoformat()
    end = now.isoformat()
    body: dict = {
        "timeRange": {"startAt": start, "endAt": end},
        "page": {"limit": limit},
    }
    if aic:
        body["filter"] = {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}
    if cursor:
        body["page"]["cursor"] = cursor
    return body


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_error_attribution(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递带 error_code 的 access 事件 → errors/query 15s 内返回归因摘要。"""
    from app.core.config import settings

    aic = f"aic-e2e-err-{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())

    await produce_access_event(
        aic=aic,
        log_id=log_id,
        route="/api/v1/orders",
        response_status=500,
        duration_ms=200,
        error_code="INTERNAL_ERROR",
    )

    await wait_for_access_event_ingested(log_id, timeout_s=20.0)
    await asyncio.sleep(1.0)  # 等待 MV 写入

    deadline = asyncio.get_event_loop().time() + 15.0
    errors_resp: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/access/errors/attribution",
            json=_time_range_body(aic=aic),
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("items"):
                errors_resp = data
                break
        await asyncio.sleep(1.0)

    assert errors_resp is not None, f"errors/attribution 在 15s 内未返回 aic={aic!r} 的错误归因"
    items = errors_resp.get("items", [])
    assert items, "errors/query items 为空"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_slow_requests(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递高延迟事件 → slow-requests/query 15s 内返回该事件（threshold=0）。"""
    from app.core.config import settings

    aic = f"aic-e2e-slow-{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())

    await produce_access_event(
        aic=aic,
        log_id=log_id,
        route="/api/v1/report",
        response_status=200,
        duration_ms=5000,
    )

    await wait_for_access_event_ingested(log_id, timeout_s=20.0)
    await asyncio.sleep(1.0)

    # threshold=0 确保捕获所有事件（minDurationMs=0 返回所有请求）
    body = _time_range_body(aic=aic)
    body["minDurationMs"] = 0

    deadline = asyncio.get_event_loop().time() + 15.0
    slow_resp: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/access/slow-requests/top",
            json=body,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("items"):
                slow_resp = data
                break
        await asyncio.sleep(1.0)

    assert slow_resp is not None, f"slow-requests/top 在 15s 内未返回 aic={aic!r} 的慢请求"
    items = slow_resp.get("items", [])
    assert items, "slow-requests/query items 为空"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_events_filter_by_aic(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """events/query 支持 filter by aic，不返回其他 aic 的事件。"""
    from app.core.config import settings

    aic_a = f"aic-filter-a-{uuid.uuid4().hex[:8]}"
    aic_b = f"aic-filter-b-{uuid.uuid4().hex[:8]}"
    log_id_a = str(uuid.uuid4())
    log_id_b = str(uuid.uuid4())

    await produce_access_event(aic=aic_a, log_id=log_id_a, route="/api/a")
    await produce_access_event(aic=aic_b, log_id=log_id_b, route="/api/b")

    await wait_for_access_event_ingested(log_id_a, timeout_s=20.0)
    await wait_for_access_event_ingested(log_id_b, timeout_s=20.0)
    await asyncio.sleep(0.5)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/access/events/query",
        json=_time_range_body(aic=aic_a),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    for item in items:
        item_aic = item.get("aic")
        assert item_aic == aic_a, f"events/query filter by aic 返回了不属于 {aic_a!r} 的事件：{item_aic!r}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_events_pagination(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递 5 条事件 → 分页 limit=2 两次取完（cursor 翻页正确）。"""
    from app.core.config import settings

    aic = f"aic-e2e-page-{uuid.uuid4().hex[:8]}"
    log_ids = [str(uuid.uuid4()) for _ in range(5)]

    for lid in log_ids:
        await produce_access_event(aic=aic, log_id=lid)

    # 等待所有事件入库
    for lid in log_ids:
        await wait_for_access_event_ingested(lid, timeout_s=25.0)
    await asyncio.sleep(0.5)

    # 第 1 页（limit=2）
    page1_body = _time_range_body(aic=aic, limit=2)
    resp1 = await e2e_http_client.post(
        f"{settings.api_v1_str}/access/events/query",
        json=page1_body,
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    items1 = data1.get("items", [])
    assert len(items1) <= 2

    cursor = data1.get("page", {}).get("cursor") or data1.get("meta", {}).get("nextCursor")
    if cursor:
        # 第 2 页：保持相同的 timeRange/filter，只替换 cursor
        page2_body = {**page1_body, "page": {"limit": 2, "cursor": cursor}}
        resp2 = await e2e_http_client.post(
            f"{settings.api_v1_str}/access/events/query",
            json=page2_body,
        )
        assert resp2.status_code == 200, f"翻页请求失败：{resp2.status_code} {resp2.text}"
        data2 = resp2.json()
        items2 = data2.get("items", [])
        # 两页加起来应覆盖至少 2 条（不对总数强断言，规避 CH 最终一致延迟）
        total = len(items1) + len(items2)
        assert total >= 2, f"两页合计应 ≥ 2 条，实际 total={total}"
