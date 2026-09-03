from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from cachetools import TTLCache
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, Response
from starlette.middleware.base import RequestResponseEndpoint

from app.core.config import Settings
from app.main import (
    AUTH_LISTENER,
    GROUP_LISTENER,
    ListenerName,
    ServiceContainer,
    create_app,
)
from app.services.authz import AuthorizationService
from app.services.group_acl import GroupAclService, StoreUnavailableError

VALID_LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
VALID_MEMBER_AIC = "1.2.156.3088.1.1.89AB.123456.7LMNOP.1ABC"
VALID_OTHER_AIC = "1.2.156.3088.1.1.CDEF.654321.ZYXWVU.2DEF"
VALID_GROUP_ID = "group-20260414-abc123"


async def _yield_control() -> None:
    await asyncio.sleep(0)


class InMemoryGroupAclStore:
    def __init__(self) -> None:
        self.groups: dict[str, set[str]] = {}
        self.expire_calls: list[tuple[str, int]] = []
        self.unavailable = False

    def _ensure_available(self) -> None:
        if self.unavailable:
            raise StoreUnavailableError("redis unavailable")

    async def ping(self) -> bool:
        await _yield_control()
        return not self.unavailable

    async def sismember(self, key: str, member: str) -> bool:
        await _yield_control()
        self._ensure_available()
        return member in self.groups.get(key, set())

    async def smembers(self, key: str) -> set[str]:
        await _yield_control()
        self._ensure_available()
        return set(self.groups.get(key, set()))

    async def sadd(self, key: str, member: str) -> None:
        await _yield_control()
        self._ensure_available()
        self.groups.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        await _yield_control()
        self._ensure_available()
        members = self.groups.get(key)
        if members is None:
            return
        members.discard(member)
        if not members:
            self.groups.pop(key, None)

    async def delete(self, key: str) -> None:
        await _yield_control()
        self._ensure_available()
        self.groups.pop(key, None)

    async def expire(self, key: str, ttl_seconds: int) -> None:
        await _yield_control()
        self._ensure_available()
        self.expire_calls.append((key, ttl_seconds))

    async def aclose(self) -> None:
        await _yield_control()
        return


class SyncASGIClient:
    """同步包装的 ASGI client，替代已弃用的 TestClient。"""

    def __init__(
        self,
        app: FastAPI,
        *,
        base_url: str,
        raise_app_exceptions: bool = True,
    ) -> None:
        self._app = app
        self._base_url = base_url
        self._raise_app_exceptions = raise_app_exceptions
        self.app = app

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        async def _send() -> Response:
            transport = ASGITransport(
                app=self._app,
                raise_app_exceptions=self._raise_app_exceptions,
            )
            async with AsyncClient(transport=transport, base_url=self._base_url) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_send())

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)


@dataclass
class FakeRabbitMqManagementClient:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def delete_connections_by_username(self, *, username: str, reason: str) -> None:
        await _yield_control()
        self.calls.append((username, reason))

    async def aclose(self) -> None:
        await _yield_control()
        return


def build_test_client(
    *,
    listener_port: int,
    store: InMemoryGroupAclStore | None = None,
    management_client: FakeRabbitMqManagementClient | None = None,
    caller_aic: str | None = None,
) -> tuple[SyncASGIClient, GroupAclService, InMemoryGroupAclStore, FakeRabbitMqManagementClient]:
    resolved_store = store or InMemoryGroupAclStore()
    resolved_mgmt = management_client or FakeRabbitMqManagementClient()
    group_acl_service = GroupAclService(
        store=resolved_store,
        management_client=resolved_mgmt,
        local_cache=TTLCache(maxsize=128, ttl=30),
        key_ttl_seconds=7 * 24 * 60 * 60,
    )
    services = ServiceContainer(
        authz_service=AuthorizationService(group_acl_service),
        group_acl_service=group_acl_service,
    )
    settings = Settings.model_validate({"RABBITMQ_MGMT_PASS": "test-pass"})
    listener_name: ListenerName
    if listener_port == settings.group_api_port:
        listener_name = GROUP_LISTENER
    elif listener_port == settings.auth_api_port:
        listener_name = AUTH_LISTENER
    else:
        raise ValueError(f"unsupported listener port: {listener_port}")

    app = create_app(listener_name=listener_name, settings=settings, services=services)

    @app.middleware("http")
    async def inject_listener_port(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Any:
        request.scope["server"] = ("testserver", listener_port)
        return await call_next(request)

    if caller_aic is not None:

        @app.middleware("http")
        async def inject_peer_common_name(
            request: Request,
            call_next: RequestResponseEndpoint,
        ) -> Any:
            request.state.peer_common_name = caller_aic
            return await call_next(request)

    client = SyncASGIClient(
        app,
        base_url=f"https://testserver:{listener_port}",
    )
    return client, group_acl_service, resolved_store, resolved_mgmt


@pytest.fixture
def group_cache_key() -> str:
    return GroupAclService.build_cache_key(VALID_LEADER_AIC, VALID_GROUP_ID)
