"""AccessRequest.route 字段序列化与缺省测试。

TDD A-1：先写测试（红）→ 在 models.py 补 route 字段（绿）。
"""

from __future__ import annotations

import pytest

from acps_sdk.amp.models import AccessBody, AccessRequest


class TestAccessRequestRouteField:
    """AccessRequest.route 字段行为测试。"""

    def test_route_present_in_model_dump_by_alias(self) -> None:
        """route 序列化时 key 为 'route'（无 alias，保持 snake_case）。"""
        req = AccessRequest(route="/users/{id}")
        data = req.model_dump(by_alias=True, exclude_none=True)
        assert "route" in data
        assert data["route"] == "/users/{id}"

    def test_route_present_in_model_dump(self) -> None:
        """route 在不带 by_alias 时同样存在。"""
        req = AccessRequest(route="/users/{id}")
        data = req.model_dump(exclude_none=True)
        assert "route" in data
        assert data["route"] == "/users/{id}"

    def test_route_defaults_to_none(self) -> None:
        """AccessRequest 不传 route 时，route 属性为 None。"""
        req = AccessRequest(method="GET")
        assert req.route is None

    def test_route_and_url_coexist(self) -> None:
        """route 与 url 字段并存时互不影响。"""
        req = AccessRequest(url="/users/123", route="/users/{id}")
        assert req.url == "/users/123"
        assert req.route == "/users/{id}"

    def test_route_field_position_after_url(self) -> None:
        """route 字段位置在 url 之后（保持 spec §5.4.1 字段顺序）。"""
        fields = list(AccessRequest.model_fields.keys())
        url_idx = fields.index("url")
        route_idx = fields.index("route")
        assert route_idx > url_idx, f"route ({route_idx}) 应在 url ({url_idx}) 之后"

    def test_model_validate_with_route(self) -> None:
        """从 dict 反序列化时 route 正常取值。"""
        req = AccessRequest.model_validate({"method": "GET", "url": "/items", "route": "/items"})
        assert req.route == "/items"

    def test_model_validate_without_route(self) -> None:
        """旧格式 dict（无 route 键）反序列化时 route 为 None（向后兼容）。"""
        req = AccessRequest.model_validate({"method": "GET"})
        assert req.route is None

    def test_access_body_with_route_in_request(self) -> None:
        """AccessBody.model_validate 对含 route 的 body 正常通过。"""
        body = AccessBody.model_validate(
            {
                "durationMs": 12.5,
                "request": {"method": "POST", "url": "/orders/456", "route": "/orders/{id}"},
            }
        )
        assert body.request is not None
        assert body.request.route == "/orders/{id}"

    def test_access_body_without_route_in_request(self) -> None:
        """AccessBody.model_validate 对不含 route 的 body 正常通过（向后兼容）。"""
        body = AccessBody.model_validate(
            {
                "durationMs": 5.0,
                "request": {"method": "GET", "url": "/health"},
            }
        )
        assert body.request is not None
        assert body.request.route is None

    @pytest.mark.parametrize(
        "route",
        [
            "/users/{id}",
            "/api/v1/orders/{orderId}/items/{itemId}",
            "SomeRpcService/Method",
            None,
        ],
    )
    def test_route_roundtrip(self, route: str | None) -> None:
        """route 值往返序列化与反序列化无损。"""
        req = AccessRequest(route=route)
        data = req.model_dump(mode="json", by_alias=True)
        restored = AccessRequest.model_validate(data)
        assert restored.route == route
