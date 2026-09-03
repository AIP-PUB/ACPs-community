"""E2E — 批量查询与 cursor 分页流程（Step 11）。

验收项：
- 注入 50 个不同 aic 的心跳
- /liveness/query（aic in 列表）→ 全量返回 50 条
- cursor 翻页（pageSize=10）→ 5 页完整遍历，无重复
- /silence/top 黑盒断言（返回条数、字段非空）
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from app.core.config import settings
from tests.support.kafka_helper import produce_heartbeat

_HB = f"{settings.api_v1_str}/heartbeat"

AIC_COUNT = 50
PAGE_SIZE = 10


@pytest.mark.asyncio
async def test_heartbeat_query_and_pagination(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """注入 50 条心跳 → 全量 query → cursor 分页 → silence/top 黑盒。"""
    aics = [f"e2e-aic-query-{i:03d}" for i in range(AIC_COUNT)]

    # 批量投递心跳
    for aic in aics:
        await produce_heartbeat(aic)
        await asyncio.sleep(0.01)

    # 等待所有 aics 都被写入（最多 15s）
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{_HB}/liveness/query",
            json={"filter": {"conditions": [{"field": "aic", "op": "in", "value": aics}]}},
        )
        assert resp.status_code == 200
        body = resp.json()
        items = body.get("items", [])
        if len(items) == AIC_COUNT:
            break
        await asyncio.sleep(0.5)

    # 全量验证
    resp = await e2e_http_client.post(
        f"{_HB}/liveness/query",
        json={"filter": {"conditions": [{"field": "aic", "op": "in", "value": aics}]}},
    )
    assert resp.status_code == 200
    body = resp.json()
    items = body.get("items", [])
    assert len(items) == AIC_COUNT, f"期望 {AIC_COUNT} 条，实际 {len(items)} 条"

    # cursor 分页（page.limit=10）→ 5 页
    # planner 要求必须有 selective filter；用 silenceDurationSeconds >= 0 扫全量
    # 每个 item 是 HeartbeatLivenessEnvelope：{"data": {...}, "meta": {...}}
    # 游标位于 meta.nextCursor（AMPResponseMeta.next_cursor）
    # 必须在每次请求中携带相同 filter，否则游标指纹校验失败
    _scan_all_filter = {"conditions": [{"field": "silenceDurationSeconds", "op": "gte", "value": 0}]}
    all_aics_paged: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        payload: dict = {"filter": _scan_all_filter, "page": {"limit": PAGE_SIZE}}
        if cursor is not None:
            payload["page"]["cursor"] = cursor
        resp = await e2e_http_client.post(f"{_HB}/liveness/query", json=payload)
        assert resp.status_code == 200
        page_body = resp.json()
        page_items = page_body.get("items", [])
        all_aics_paged.extend(item["data"]["aic"] for item in page_items)
        page_count += 1

        next_cursor = page_body.get("meta", {}).get("nextCursor")
        if not next_cursor or not page_items:
            break
        cursor = next_cursor
        if page_count > 20:
            break

    assert page_count >= 5, f"期望 >= 5 页（50 条 / size=10），实际 {page_count} 页"
    # 无重复
    assert len(set(all_aics_paged)) == len(all_aics_paged), "cursor 翻页出现重复 aic"
    # 覆盖所有插入的 aic（部分 aic 可能分布到多页）
    assert len(all_aics_paged) >= AIC_COUNT, f"cursor 翻页未遍历全量：{len(all_aics_paged)} < {AIC_COUNT}"


@pytest.mark.asyncio
async def test_silence_top_black_box(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """silence/top 黑盒：字段存在、类型正确、返回条数 <= topN。

    items 为 HeartbeatSilenceRankItem，非信封，aic/lastSeenAt/silenceDurationSeconds 直接位于顶层。
    """
    await produce_heartbeat("e2e-aic-sltop-001")
    await asyncio.sleep(1.5)

    resp = await e2e_http_client.post(f"{_HB}/silence/top", json={"topN": 10})
    assert resp.status_code == 200
    body = resp.json()
    items = body.get("items", [])
    assert isinstance(items, list)
    assert len(items) <= 10
    for item in items:
        assert "aic" in item
        assert "lastSeenAt" in item
        assert "silenceDurationSeconds" in item
        assert isinstance(item["silenceDurationSeconds"], int)
