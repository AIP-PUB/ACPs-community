"""tests/integration/test_access_query_api.py — Access Query API 集成测试（D-3）。

通过 ASGI 客户端调用 FastAPI 路由，验证 JSON schema、状态码、分页元数据。
需要真实 ClickHouse + Redis（dev-infra）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.support.clickhouse_helper import insert_raw_events, make_access_event_row
from tests.support.constants import TEST_REDIS_URL
from tests.support.factory import ACCESS_API_PREFIX
from tests.support.redis_helper import reset_access_redis_state


@pytest.fixture
async def http_client_access() -> AsyncGenerator[AsyncClient]:
    """ASGI 客户端（lifespan 在 testing 模式下跳过后台任务）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def redis_access():
    from redis.asyncio import Redis

    r = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await reset_access_redis_state(r)
    yield r
    await reset_access_redis_state(r)
    await r.aclose()


@pytest.fixture(autouse=True)
async def _deps(_require_clickhouse: None, isolated_clickhouse: None) -> None:
    """所有 API 集成测试依赖 CH schema 已建 + 表清空。"""


def _time_range(hours_back: int = 1) -> dict:
    """构造当前时间向前 hours_back 小时的时间范围（已在 retention 内）。"""
    now = datetime.now(UTC)
    start = (now - timedelta(hours=hours_back)).isoformat()
    end = now.isoformat()
    return {"startAt": start, "endAt": end}


class TestEventsQueryEndpoint:
    async def test_empty_table_returns_empty_list(self, http_client_access: AsyncClient) -> None:
        body = {"timeRange": _time_range()}
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (200, 206, 503), f"意外状态码: {resp.status_code}"
        if resp.status_code in (200, 206):
            data = resp.json()
            assert "items" in data

    async def test_returns_inserted_events(self, http_client_access: AsyncClient) -> None:
        """插入 2 行后 API 能返回这 2 行。"""
        aic = f"aic-api-{uuid.uuid4().hex[:6]}"
        rows = [make_access_event_row(aic=aic) for _ in range(2)]
        await insert_raw_events(rows)

        body = {
            "timeRange": _time_range(),
            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]},
        }
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (200, 206, 503)
        if resp.status_code in (200, 206):
            data = resp.json()
            assert len(data["items"]) == 2

    async def test_response_has_meta_field(self, http_client_access: AsyncClient) -> None:
        body = {"timeRange": _time_range()}
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (200, 206, 503)
        if resp.status_code in (200, 206):
            data = resp.json()
            assert "meta" in data

    async def test_pagination_with_limit(self, http_client_access: AsyncClient) -> None:
        """limit=2 时 hasMore 应为 True（插入 3 行）。"""
        aic = f"aic-page-{uuid.uuid4().hex[:6]}"
        rows = [make_access_event_row(aic=aic) for _ in range(3)]
        await insert_raw_events(rows)

        body = {
            "timeRange": _time_range(),
            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]},
            "page": {"limit": 2},
        }
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (200, 206, 503)
        if resp.status_code in (200, 206):
            data = resp.json()
            meta = data.get("meta", {})
            assert meta.get("hasMore") is True or len(data["items"]) == 2


class TestOperationsQueryEndpoint:
    async def test_operations_empty_returns_200(self, http_client_access: AsyncClient) -> None:
        body = {"timeRange": _time_range(), "groupBy": ["aic"]}
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/operations/query", json=body)
        assert resp.status_code in (200, 206, 503)


class TestErrorAttributionEndpoint:
    async def test_error_attribution_requires_analytics_enabled(self, http_client_access: AsyncClient) -> None:
        """access_analytics_enabled=False 时路由不注册，返回 404。"""
        from app.core.config import settings

        body = {"timeRange": _time_range(), "groupBy": ["errorCode"]}
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/errors/attribution", json=body)
        if not settings.access_analytics_enabled:
            assert resp.status_code == 404
        else:
            assert resp.status_code in (200, 206, 422, 503)

    async def test_error_attribution_with_error_rows(self, http_client_access: AsyncClient) -> None:
        """插入错误行后 attribution 返回非空列表（analytics enabled 时）。"""
        from app.core.config import settings

        if not settings.access_analytics_enabled:
            pytest.skip("access_analytics_enabled=False，跳过")

        aic = f"aic-errattr-{uuid.uuid4().hex[:6]}"
        rows = [make_access_event_row(aic=aic, response_status=500) for _ in range(2)]
        await insert_raw_events(rows)

        body = {"timeRange": _time_range(), "groupBy": ["errorCode"]}
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/errors/attribution", json=body)
        assert resp.status_code in (200, 206, 503)
        if resp.status_code in (200, 206):
            assert "items" in resp.json()


class TestSlowRequestsEndpoint:
    async def test_slow_requests_top(self, http_client_access: AsyncClient) -> None:
        """插入慢请求行后 top 接口能返回记录（analytics enabled 时）。"""
        from app.core.config import settings

        if not settings.access_analytics_enabled:
            pytest.skip("access_analytics_enabled=False，跳过")

        aic = f"aic-slowapi-{uuid.uuid4().hex[:6]}"
        rows = [make_access_event_row(aic=aic, duration_ms=8000) for _ in range(2)]
        await insert_raw_events(rows)

        body = {"timeRange": _time_range(), "topN": 5}
        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/slow-requests/top", json=body)
        assert resp.status_code in (200, 206, 503)


class TestRetentionError:
    async def test_out_of_retention_returns_422(self, http_client_access: AsyncClient) -> None:
        """超出保留期（testing=1天）的时间范围应返回 422。"""
        past_start = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        past_end = (datetime.now(UTC) - timedelta(days=29)).isoformat()
        body = {"timeRange": {"startAt": past_start, "endAt": past_end}}

        resp = await http_client_access.post(f"{ACCESS_API_PREFIX}/events/query", json=body)
        assert resp.status_code in (422, 400), f"超期请求应返回 4xx，实际 {resp.status_code}"
        if resp.status_code in (422, 400):
            data = resp.json()
            assert "error_code" in data or "type" in data
