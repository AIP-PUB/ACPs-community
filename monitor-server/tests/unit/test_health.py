"""tests/unit/test_health.py — /health 端点单元测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestHealthEndpoint:
    async def test_health_returns_200_when_all_ok(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["redis"] == "ok"
        assert data["checks"]["clickhouse"] == "ok"

    async def test_health_returns_503_when_db_down(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=False)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["checks"]["database"] == "error"
        assert data["checks"]["redis"] == "ok"

    async def test_health_returns_503_when_redis_down(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=False)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["redis"] == "error"

    async def test_health_response_contains_service_name(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        data = resp.json()
        assert "AMP Monitor Server" in data["service"]

    async def test_health_vm_ok(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["checks"]["victoria_metrics"] == "ok"

    async def test_health_vm_error(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=False)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["victoria_metrics"] == "error"

    async def test_health_clickhouse_error(self, client: AsyncClient) -> None:
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=False)),
            patch("app.main._check_os", AsyncMock(return_value=True)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["clickhouse"] == "error"
        assert resp.json()["status"] == "degraded"

    async def test_health_opensearch_error(self, client: AsyncClient) -> None:
        """OpenSearch 不可达 → opensearch: error（不影响整体 status，System 模块不可用时其余模块正常）。"""
        with (
            patch("app.main.check_database", AsyncMock(return_value=True)),
            patch("app.main.check_redis", AsyncMock(return_value=True)),
            patch("app.main._check_vm", AsyncMock(return_value=True)),
            patch("app.main._check_ch", AsyncMock(return_value=True)),
            patch("app.main._check_os", AsyncMock(return_value=False)),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 200  # 其余模块仍可服务，不降级整体 status
        assert resp.json()["checks"]["opensearch"] == "error"
        assert resp.json()["status"] == "ok"  # 整体仍 ok，仅 opensearch 检查项报 error
