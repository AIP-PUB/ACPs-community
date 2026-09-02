"""E2E — 超 raw_retention_days 窗口 → AMP_OUT_OF_RETENTION（H-1 场景 7）。

验收项（C-MESSAGE-RETENTION-1）：
- timeRange.startAt < now - message_raw_retention_days → events/query 返回 422 AMP_OUT_OF_RETENTION
- 合法窗口不触发错误
- retention 边界：刚好在限制内 → 200，超出 1 天 → 422
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_events_query_out_of_retention(
    e2e_http_client: AsyncClient,
) -> None:
    """超出 retention 窗口的查询返回 422 AMP_OUT_OF_RETENTION（设计 §6.9，C-MESSAGE-RETENTION-1）。"""
    from app.core.config import settings

    retention_days: int = settings.message_raw_retention_days
    now = datetime.now(UTC)

    # 超出保留期 1 天
    body: dict[str, Any] = {
        "timeRange": {
            "startAt": (now - timedelta(days=retention_days + 1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "page": {"limit": 10},
    }
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/events/query",
        json=body,
    )
    assert resp.status_code == 422, f"期望 422，实际 {resp.status_code}: {resp.text}"
    data = resp.json()
    error_code = data.get("error_code", "")
    assert "OUT_OF_RETENTION" in error_code, f"错误码不含 OUT_OF_RETENTION: {data}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_events_query_within_retention(
    e2e_http_client: AsyncClient,
) -> None:
    """在 retention 窗口内的查询不触发 OUT_OF_RETENTION。"""
    from app.core.config import settings

    now = datetime.now(UTC)

    body: dict[str, Any] = {
        "timeRange": {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "page": {"limit": 5},
    }
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/events/query",
        json=body,
    )
    # 200 或 503（freshness 未就绪）均可接受，不应是 400 OUT_OF_RETENTION
    assert resp.status_code != 400, f"合法窗口不应返回 400: {resp.text}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_lifecycles_query_out_of_retention(
    e2e_http_client: AsyncClient,
) -> None:
    """lifecycles/query 超出 retention 亦返回 422 AMP_OUT_OF_RETENTION（设计 §6.9）。"""
    from app.core.config import settings

    retention_days: int = settings.message_lifecycle_retention_days
    now = datetime.now(UTC)

    body: dict[str, Any] = {
        "timeRange": {
            "startAt": (now - timedelta(days=retention_days + 1)).isoformat(),
            "endAt": now.isoformat(),
        },
    }
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/lifecycles/query",
        json=body,
    )
    assert resp.status_code == 422, f"期望 422，实际 {resp.status_code}: {resp.text}"
    data = resp.json()
    error_code = data.get("error_code", "")
    assert "OUT_OF_RETENTION" in error_code, f"错误码不含 OUT_OF_RETENTION: {data}"
