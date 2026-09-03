"""RequestIdMiddleware 的单元测试。

覆盖：
- 无 X-Request-ID 请求头时自动生成 UUID4
- 有 X-Request-ID 请求头时沿用原值（链路追踪透传）
- 响应头中包含 X-Request-ID
- 不同请求生成不同 ID
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient, Response

from app.core.middleware import RequestIdMiddleware


def make_app() -> FastAPI:
    """创建挂载了 RequestIdMiddleware 的最小 FastAPI 应用。"""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    async def ping() -> PlainTextResponse:
        return PlainTextResponse("pong")

    return app


def _get(app: FastAPI, path: str, **kwargs: Any) -> Response:
    async def _send() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path, **kwargs)

    return asyncio.run(_send())


class TestRequestIdMiddleware:
    """RequestIdMiddleware 的行为测试。"""

    def test_generates_uuid4_when_header_absent(self) -> None:
        response = _get(make_app(), "/ping")
        assert response.status_code == 200
        assert "X-Request-ID" in response.headers
        request_id = response.headers["X-Request-ID"]
        # 验证是合法的 UUID 格式
        parsed = uuid.UUID(request_id)
        assert str(parsed) == request_id

    def test_reuses_request_id_from_incoming_header(self) -> None:
        custom_id = "my-upstream-trace-id-12345"
        response = _get(make_app(), "/ping", headers={"X-Request-ID": custom_id})
        assert response.headers["X-Request-ID"] == custom_id

    def test_response_header_present_even_when_not_provided(self) -> None:
        response = _get(make_app(), "/ping")
        assert "X-Request-ID" in response.headers

    def test_different_requests_get_different_ids(self) -> None:
        app = make_app()
        resp1 = _get(app, "/ping")
        resp2 = _get(app, "/ping")
        assert resp1.headers["X-Request-ID"] != resp2.headers["X-Request-ID"]

    def test_endpoint_still_returns_200(self) -> None:
        response = _get(make_app(), "/ping")
        assert response.status_code == 200
        assert response.text == "pong"
