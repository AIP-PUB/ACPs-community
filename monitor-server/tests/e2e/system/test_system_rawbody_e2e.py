"""E2E — includeRawLog 与内部字段边界（H-1 rawbody 场景）。

验证：
- includeRawLog=true 返回 rawBody
- 默认不返回 rawBody
- search_text / indexed_at 永不出参（C-SYSTEM-QUERY-3）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested


def _query_body(*, log_id: str, include_raw_log: bool) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "timeRange": {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "filter": {
            "conditions": [{"field": "logId", "op": "eq", "value": log_id}],
            "logic": "and",
        },
        "page": {"limit": 10},
        "includeRawLog": include_raw_log,
    }


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_include_raw_log_true_returns_raw_body(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """includeRawLog=true 时响应含 rawBody。"""
    from app.core.config import settings

    log_id = await produce_system_event(message="rawbody-include-test")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=_query_body(log_id=log_id, include_raw_log=True),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0].get("rawBody") is not None
    assert "message" in items[0]["rawBody"]


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_include_raw_log_false_omits_raw_body(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """默认 includeRawLog=false 时不返回 rawBody。"""
    from app.core.config import settings

    log_id = await produce_system_event(message="rawbody-omit-test")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=_query_body(log_id=log_id, include_raw_log=False),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0].get("rawBody") is None


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_response_never_contains_internal_fields(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """响应中不含 search_text / indexed_at（任何 includeRawLog 值）。"""
    from app.core.config import settings

    log_id = await produce_system_event(message="internal-fields-test")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    for include_raw in (True, False):
        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/system/events/query",
            json=_query_body(log_id=log_id, include_raw_log=include_raw),
        )
        assert resp.status_code == 200
        for item in resp.json().get("items", []):
            assert "search_text" not in item
            assert "searchText" not in item
            assert "indexed_at" not in item
            assert "indexedAt" not in item
