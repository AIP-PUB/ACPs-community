"""E2E — Message 游标分页（H-1 场景 6）。

验收项：
- 投递 N 条 send 事件 → events/query page.limit=2 → 多次翻页直到 nextCursor 消失
- 最终累计 items 数量 == N（无重复无遗漏）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_events_cursor_pagination(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """投递 5 条事件，以 limit=2 翻页，最终累计 5 行无重复。"""
    from app.core.config import settings

    n = 5
    tag = uuid.uuid4().hex[:8]
    aic = f"aic-page-{tag}"
    log_ids = []

    for _ in range(n):
        log_id = str(uuid.uuid4())
        log_ids.append(log_id)
        await produce_message_event(log_id=log_id, aic=aic, event_type="send")

    await wait_for_message_event_ingested(log_ids[-1], timeout_s=25)

    now = datetime.now(UTC)
    all_log_ids: list[str] = []
    cursor: str | None = None
    page_limit = 2

    for _ in range(n + 2):  # 最多翻 n+2 页防死循环
        body: dict[str, Any] = {
            "timeRange": {
                "startAt": (now - timedelta(hours=1)).isoformat(),
                "endAt": now.isoformat(),
            },
            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}], "logic": "and"},
            "page": {"limit": page_limit},
        }
        if cursor:
            body["page"]["cursor"] = cursor

        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/message/events/query",
            json=body,
        )
        assert resp.status_code == 200
        data = resp.json()
        items = data.get("items", [])
        all_log_ids.extend(item.get("logId") for item in items)

        cursor = data.get("meta", {}).get("nextCursor")
        if not cursor:
            break

    assert len(all_log_ids) == n, f"翻页累计 {len(all_log_ids)} 行，期望 {n}"
    assert len(set(all_log_ids)) == n, "翻页结果有重复行"
