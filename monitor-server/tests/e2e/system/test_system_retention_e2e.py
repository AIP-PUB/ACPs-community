"""E2E — System 保留窗口护栏（H-1 retention 场景，C-SYSTEM-RETENTION-1）。

验收项：
- 超出 archive_retention_days → AMP_OUT_OF_RETENTION（422，不静默空）
- 近期窗口内查询不误判
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.e2e.system.helpers import system_time_range_body


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_out_of_retention_returns_422(
    e2e_http_client: AsyncClient,
) -> None:
    """startAt 早于保留窗口 → AMP_OUT_OF_RETENTION。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode

    now = datetime.now(UTC)
    old_start = (now - timedelta(days=settings.system_archive_retention_days + 10)).isoformat()
    old_end = (now - timedelta(days=settings.system_archive_retention_days + 5)).isoformat()

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(start_at=datetime.fromisoformat(old_start), end_at=datetime.fromisoformat(old_end)),
    )
    assert resp.status_code == 422
    assert resp.json().get("error_code") == SystemErrorCode.OUT_OF_RETENTION


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_within_retention_window_not_rejected(
    e2e_http_client: AsyncClient,
) -> None:
    """近期时间窗口内查询不被 OUT_OF_RETENTION 拒绝。"""
    from app.core.config import settings
    from tests.support.opensearch_helper import create_test_index

    await create_test_index()
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(lookback_hours=1),
    )
    # 无数据时 200 空列表；无水位时可能 503 lagging——但不应是 422 OUT_OF_RETENTION
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        assert "items" in resp.json()
    else:
        assert resp.json().get("error_code") != "AMP_OUT_OF_RETENTION"
