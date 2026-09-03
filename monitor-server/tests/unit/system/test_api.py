"""tests/unit/system/test_api.py — api.py 路由单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.amp_api_schema import AMPResponseMeta
from app.system.schema import SystemEventView


def _make_meta() -> AMPResponseMeta:
    return AMPResponseMeta()


def _make_view() -> SystemEventView:
    return SystemEventView(
        log_id="log-001",
        timestamp="2024-06-14T12:00:00Z",
        aic="aic-001",
        severity_number=0,
        message="test",
    )


class TestSystemApiRouter:
    def test_router_prefix_is_system(self) -> None:
        from app.system.api import router

        assert router.prefix == "/system"

    def test_events_query_endpoint_registered(self) -> None:
        from app.system.api import router

        paths = [route.path for route in router.routes if isinstance(route, APIRoute)]
        assert any("/events/query" in p for p in paths)

    @pytest.mark.asyncio
    async def test_query_events_returns_200(self) -> None:
        """POST /system/events/query 正常请求返回 200。"""
        from datetime import UTC, datetime, timedelta

        from fastapi import FastAPI

        from app.system.api import router

        test_app = FastAPI()
        test_app.include_router(router)

        now = datetime.now(UTC)
        start_at = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with patch("app.system.service.query_events", return_value=([_make_view()], _make_meta())):
            with patch("app.core.redis_client.get_redis", return_value=AsyncMock()):
                client = TestClient(test_app, raise_server_exceptions=True)
                response = client.post(
                    "/system/events/query",
                    json={"timeRange": {"startAt": start_at, "endAt": end_at}},
                )

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "meta" in data

    @pytest.mark.asyncio
    async def test_query_events_app_error_returns_problem_details(self) -> None:
        """AppError → Problem Details 响应（error_code 字段存在）。"""
        from fastapi import FastAPI

        from app.core.base_exception import register_exception_handlers
        from app.system.api import router
        from app.system.exception import SystemKeywordTooBroadError

        test_app = FastAPI()
        register_exception_handlers(test_app)
        test_app.include_router(router)

        with (
            patch(
                "app.system.service.query_events",
                side_effect=SystemKeywordTooBroadError("keyword too short"),
            ),
            patch("app.core.redis_client.get_redis", return_value=AsyncMock()),
        ):
            client = TestClient(test_app, raise_server_exceptions=False)
            response = client.post(
                "/system/events/query",
                json={"timeRange": {"startAt": "2024-01-01T00:00:00Z", "endAt": "2024-01-02T00:00:00Z"}},
            )

        assert response.status_code == 422
        body = response.json()
        assert "error_code" in body or "type" in body  # Problem Details 字段

    @pytest.mark.asyncio
    async def test_query_events_disabled_returns_404(self) -> None:
        """system_query_enabled=false 时端点返回 404（只写部署，§7.2）。"""
        from unittest.mock import patch

        from fastapi import FastAPI

        from app.system.api import router

        test_app = FastAPI()
        test_app.include_router(router)

        with patch("app.core.config.settings") as mock_settings:
            mock_settings.system_query_enabled = False

            with patch("app.core.redis_client.get_redis", return_value=AsyncMock()):
                client = TestClient(test_app, raise_server_exceptions=False)
                response = client.post(
                    "/system/events/query",
                    json={"timeRange": {"startAt": "2024-01-01T00:00:00Z", "endAt": "2024-01-02T00:00:00Z"}},
                )

        assert response.status_code == 404
