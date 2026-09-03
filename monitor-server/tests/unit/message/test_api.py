"""单元测试：G-2 api.py — 路由注册与端点行为（设计 §6.24）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app_with_message_router() -> FastAPI:
    from app.message.api import router

    app = FastAPI()
    app.include_router(router, prefix="/acps-amp-v1")
    return app


class TestRouterBasics:
    def test_router_has_prefix_message(self) -> None:
        from app.message.api import router

        assert router.prefix == "/message"

    def test_events_query_always_registered(self) -> None:
        from app.message.api import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert any("events/query" in p for p in paths)

    def test_lifecycles_query_registered_when_reliability_enabled(self) -> None:
        from app.core.config import settings
        from app.message.api import router

        if settings.message_reliability_enabled:
            paths = [getattr(r, "path", "") for r in router.routes]
            assert any("lifecycles/query" in p for p in paths)

    def test_destinations_throughput_registered_when_destination_enabled(self) -> None:
        from app.core.config import settings
        from app.message.api import router

        if settings.message_destination_enabled:
            paths = [getattr(r, "path", "") for r in router.routes]
            assert any("destinations/throughput" in p for p in paths)


class TestEventsQueryEndpoint:
    @pytest.fixture
    def client(self) -> TestClient:
        from app.message.api import router

        app = FastAPI()
        app.include_router(router, prefix="/acps-amp-v1")
        return TestClient(app, raise_server_exceptions=True)

    def test_events_query_returns_200(self, client: TestClient) -> None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        time_range = {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": (now - timedelta(minutes=5)).isoformat(),
        }
        from app.message.freshness import FreshnessView
        from app.message.schema import MessageEventView

        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        view = MessageEventView(
            log_id="log-001",
            timestamp="2026-06-01T00:00:00+00:00",
            system="kafka",
            destination_name="my-topic",
            destination_kind="topic",
            event_type="send",
            direction="send",
        )
        with (
            patch("app.message.service.store.run_events_query", AsyncMock(return_value=[view])),
            patch("app.message.service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            resp = client.post(
                "/acps-amp-v1/message/events/query",
                json={"timeRange": time_range},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "meta" in data


class TestProblemDetails:
    @pytest.fixture
    def client(self) -> TestClient:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.core.base_exception import register_exception_handlers
        from app.message.api import router

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router, prefix="/acps-amp-v1")
        return TestClient(app, raise_server_exceptions=False)

    def test_out_of_retention_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/acps-amp-v1/message/events/query",
            json={"timeRange": {"startAt": "2000-01-01T00:00:00Z", "endAt": "2000-01-02T00:00:00Z"}},
        )
        assert resp.status_code in {400, 422}

    def test_missing_time_range_returns_4xx(self, client: TestClient) -> None:
        resp = client.post("/acps-amp-v1/message/events/query", json={})
        assert resp.status_code in {400, 422}
