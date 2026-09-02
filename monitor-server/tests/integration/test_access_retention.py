"""tests/integration/test_access_retention.py — 保留期策略集成测试。

验证超出保留期的查询被正确拒绝（C-ACCESS-RETENTION-1）。
需要 ClickHouse（仅用于路由注册，建表幂等）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.support.factory import ACCESS_API_PREFIX


@pytest.fixture
async def http_client_access() -> AsyncGenerator[AsyncClient]:
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _deps(_require_clickhouse: None, isolated_clickhouse: None) -> None:
    pass


def _make_body(start: datetime, end: datetime) -> dict:
    return {"timeRange": {"startAt": start.isoformat(), "endAt": end.isoformat()}}


class TestRetentionPolicy:
    async def test_within_retention_accepted(self, http_client_access: AsyncClient) -> None:
        """最近 30 分钟时间范围应被接受（200/206/503，不应 422）。"""
        now = datetime.now(UTC)
        body = _make_body(now - timedelta(minutes=30), now)
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (200, 206, 503), f"应接受近期查询，实际 {resp.status_code}"

    async def test_out_of_retention_rejected(self, http_client_access: AsyncClient) -> None:
        """超出 testing retention（1 天）的时间范围应被拒绝（4xx）。"""
        now = datetime.now(UTC)
        body = _make_body(now - timedelta(days=5), now - timedelta(days=4))
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (400, 422), f"超期应返回 4xx，实际 {resp.status_code}"
        data = resp.json()
        # RFC 9457 Problem Details + error_code
        assert "error_code" in data or "type" in data

    async def test_future_end_time_accepted(self, http_client_access: AsyncClient) -> None:
        """结束时间稍微在未来应被正常处理（not an error）。"""
        now = datetime.now(UTC)
        body = _make_body(now - timedelta(minutes=5), now + timedelta(minutes=1))
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        # 未来结束时间通常视为"当前"，不应报 4xx 保留期错误
        assert resp.status_code in (200, 206, 503)

    async def test_reversed_time_range_rejected(self, http_client_access: AsyncClient) -> None:
        """start > end 倒置时间范围应被拒绝（422 Pydantic 校验失败）。"""
        now = datetime.now(UTC)
        body = _make_body(now, now - timedelta(hours=1))  # start > end
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (400, 422)

    async def test_topology_within_retention(self, http_client_access: AsyncClient) -> None:
        """topology/query 在保留期内应被接受（apm enabled 时）。"""
        from app.core.config import settings

        if not settings.access_apm_enabled:
            pytest.skip("access_apm_enabled=False，跳过")

        now = datetime.now(UTC)
        body = _make_body(now - timedelta(hours=1), now)
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/topology/query", json=body)
        assert resp.status_code in (200, 206, 503)

    async def test_topology_out_of_retention(self, http_client_access: AsyncClient) -> None:
        """topology/query 超期应拒绝（apm enabled 时）。"""
        from app.core.config import settings

        if not settings.access_apm_enabled:
            pytest.skip("access_apm_enabled=False，跳过")

        now = datetime.now(UTC)
        # topology_retention_days = 7 in testing config; query 10+ days ago is definitely outside
        body = _make_body(now - timedelta(days=11), now - timedelta(days=10))
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/topology/query", json=body)
        assert resp.status_code in (400, 422)
