"""E2E — keyword 搜索与 search_text 投影（H-1 keyword 场景）。

验证 resource 标量（如 host.name）写入 search_text 后可被 keyword 命中（C-SYSTEM-QUERY-2）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.support.factory import make_system_log_record
from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_keyword_hits_resource_scalar_in_search_text(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """仅 message 不含关键词、resource.host.name 含唯一值 → keyword 可命中。"""
    from app.core.config import settings

    host_token = f"host-e2e-{uuid.uuid4().hex[:12]}"
    record = make_system_log_record(
        message="generic system log without host token",
        resource={
            "service.name": "e2e-system-svc",
            "host.name": host_token,
        },
    )
    log_id = await produce_system_event(record=record)
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    now = datetime.now(UTC)
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json={
            "timeRange": {
                "startAt": (now - timedelta(hours=1)).isoformat(),
                "endAt": now.isoformat(),
            },
            "keyword": host_token,
            "page": {"limit": 10},
        },
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert any(item.get("logId") == log_id for item in items), (
        f"keyword={host_token!r} 应命中 resource 投影到 search_text 的文档"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_keyword_too_short_returns_422(
    e2e_http_client: AsyncClient,
) -> None:
    """过短 keyword → AMP_SYSTEM_KEYWORD_TOO_BROAD（422）。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode

    now = datetime.now(UTC)
    short_kw = "a" * max(1, settings.system_keyword_min_length - 1)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json={
            "timeRange": {
                "startAt": (now - timedelta(hours=1)).isoformat(),
                "endAt": now.isoformat(),
            },
            "keyword": short_kw,
            "page": {"limit": 10},
        },
    )
    assert resp.status_code == 422
    assert resp.json().get("error_code") == SystemErrorCode.KEYWORD_TOO_BROAD
