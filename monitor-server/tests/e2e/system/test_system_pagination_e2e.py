"""E2E — System 游标分页 + PIT 生命周期（H-1 pagination 场景，C-SYSTEM-QUERY-5）。

验收项：
- search_after + PIT 翻页无重复无遗漏
- 跨按日索引稳定翻页
- PIT 过期 → AMP_CURSOR_INVALID
- 换参数翻页 → AMP_CURSOR_INVALID（指纹不匹配）
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.e2e.system.helpers import system_time_range_body
from tests.support.factory import make_system_log_record
from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested


async def _paginate_all(
    client: AsyncClient,
    *,
    base_body: dict[str, Any],
    expected_count: int,
    api_prefix: str,
) -> list[str]:
    all_ids: list[str] = []
    cursor: str | None = None
    for _ in range(expected_count + 3):
        body = {**base_body, "page": {**base_body.get("page", {}), "limit": 2}}
        if cursor:
            body["page"]["cursor"] = cursor
        resp = await client.post(f"{api_prefix}/system/events/query", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        all_ids.extend(item.get("logId") for item in data.get("items", []))
        cursor = data.get("meta", {}).get("nextCursor")
        if not cursor:
            break
    return all_ids


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_cursor_pagination_no_duplicates(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """N 条事件 limit=2 翻页，累计 N 行无重复（PIT + search_after）。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    aic = f"aic-pg-{tag}"
    n = 5
    last_id = ""
    for _ in range(n):
        last_id = await produce_system_event(aic=aic, message=f"page-{tag}")
    await wait_for_system_event_ingested(last_id, timeout_s=30)

    base = system_time_range_body(aic=aic, limit=2)
    all_found = await _paginate_all(
        e2e_http_client,
        base_body=base,
        expected_count=n,
        api_prefix=settings.api_v1_str,
    )
    assert len(all_found) == n
    assert len(set(all_found)) == n


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_cross_day_index_pagination(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """跨按日索引翻页：事件落在不同日索引，翻页仍无遗漏。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    aic = f"aic-xday-{tag}"
    yesterday = datetime.now(UTC) - timedelta(days=1)
    today = datetime.now(UTC)
    log_ids: list[str] = []

    for ts in (yesterday, today, today):
        record = make_system_log_record(
            aic=aic,
            message=f"xday-{tag}",
            timestamp=ts.isoformat(),
        )
        lid = await produce_system_event(record=record)
        log_ids.append(lid)

    await wait_for_system_event_ingested(log_ids[-1], timeout_s=30)
    await asyncio.sleep(2.0)

    base = system_time_range_body(
        start_at=yesterday - timedelta(hours=1),
        end_at=today + timedelta(minutes=1),
        aic=aic,
        limit=2,
    )
    all_found = await _paginate_all(
        e2e_http_client,
        base_body=base,
        expected_count=len(log_ids),
        api_prefix=settings.api_v1_str,
    )
    assert set(all_found) == set(log_ids)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_expired_pit_returns_cursor_invalid(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """主动关闭 PIT 后复用游标 → AMP_CURSOR_INVALID。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode
    from app.system.store import close_pit

    tag = uuid.uuid4().hex[:8]
    aic = f"aic-pit-{tag}"
    last_id = ""
    for i in range(4):
        last_id = await produce_system_event(aic=aic, message=f"pit-{i}")
    await wait_for_system_event_ingested(last_id, timeout_s=30)

    body = system_time_range_body(aic=aic, limit=2)
    resp1 = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=body,
    )
    assert resp1.status_code == 200
    cursor = resp1.json().get("meta", {}).get("nextCursor")
    assert cursor is not None

    payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    await close_pit(str(payload["pit"]))

    resp2 = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json={**body, "page": {"limit": 2, "cursor": cursor}},
    )
    assert resp2.status_code == 400
    assert resp2.json().get("error_code") == SystemErrorCode.CURSOR_INVALID


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_cursor_fingerprint_mismatch_returns_cursor_invalid(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """翻页时修改 filter 参数 → AMP_CURSOR_INVALID（指纹不匹配）。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode

    tag = uuid.uuid4().hex[:8]
    aic_a = f"aic-fp-a-{tag}"
    aic_b = f"aic-fp-b-{tag}"
    for _ in range(3):
        await produce_system_event(aic=aic_a, message=f"fp-a-{tag}")
    last_b = ""
    for _ in range(3):
        last_b = await produce_system_event(aic=aic_b, message=f"fp-b-{tag}")
    await wait_for_system_event_ingested(last_b, timeout_s=30)

    body_a = system_time_range_body(aic=aic_a, limit=2)
    resp1 = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=body_a,
    )
    assert resp1.status_code == 200
    cursor = resp1.json().get("meta", {}).get("nextCursor")
    assert cursor is not None

    body_b = system_time_range_body(aic=aic_b, limit=2)
    resp2 = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json={**body_b, "page": {"limit": 2, "cursor": cursor}},
    )
    assert resp2.status_code == 400
    assert resp2.json().get("error_code") == SystemErrorCode.CURSOR_INVALID
