"""E2E — Message 水位推进与 freshness headers（H-1 场景 5）。

验收项：
- 写入事件后 Redis 摄取水位（amp:message:wm:ingest:{partition_id}）前进
- events/query 响应 meta.dataFreshnessAt 有值
- 水位推进后再次查询 meta.lag_ms 减小或消失
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.message.freshness import WM_INGEST_PARTITIONS, WM_INGEST_PREFIX
from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_watermark_advances(
    e2e_message_writer: dict[str, Any],
) -> None:
    """写入事件后 Redis 摄取水位（amp:message:wm:ingest:{partition_id}）前进。

    水位键按分区分布（WM_INGEST_PREFIX + partition_id），整体水位 = min(各分区)。
    检查「分区集合非空且至少有一个分区水位 > 0」说明水位已被 Writer 推进。
    """
    redis = e2e_message_writer["redis"]

    log_id = str(uuid.uuid4())
    await produce_message_event(log_id=log_id, event_type="send")
    await wait_for_message_event_ingested(log_id, timeout_s=25)

    # 等待水位写入（Writer 在 flush 后推进水位）
    deadline = asyncio.get_event_loop().time() + 15
    wm_advanced = False
    while asyncio.get_event_loop().time() < deadline:
        partitions = await redis.smembers(WM_INGEST_PARTITIONS)
        if partitions:
            keys = [f"{WM_INGEST_PREFIX}{p.decode() if isinstance(p, bytes) else p}" for p in partitions]
            values = await redis.mget(*keys)
            if any(v and int(v) > 0 for v in values):
                wm_advanced = True
                break
        await asyncio.sleep(1)

    assert wm_advanced, "摄取水位未推进（WM_INGEST_PARTITIONS 为空或所有分区水位 = 0）"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_events_query_returns_freshness_header(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """events/query 响应头包含 X-Data-Freshness 或 meta 中有 dataFreshnessAt。"""
    from app.core.config import settings

    log_id = str(uuid.uuid4())
    await produce_message_event(log_id=log_id, event_type="send")
    await wait_for_message_event_ingested(log_id, timeout_s=25)

    now = datetime.now(UTC)
    body = {
        "timeRange": {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "page": {"limit": 10},
    }
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/events/query",
        json=body,
    )
    assert resp.status_code == 200
    data = resp.json()

    has_header = "x-data-freshness" in {k.lower() for k in resp.headers}
    has_meta = bool(data.get("meta", {}).get("dataFreshnessAt"))
    assert has_header or has_meta, "响应中无 freshness 信息"
