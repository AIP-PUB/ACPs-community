"""黑盒 E2E：monitor-server 与 Keycloak 的真实 OIDC 联调。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

import httpx
import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_heartbeat

pytestmark = [pytest.mark.e2e, pytest.mark.oidc]

_HEARTBEAT_PREFIX = "/acps-amp-v1/heartbeat"


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"未设置 {name}，跳过 OIDC 黑盒联调测试")
    return value


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _issue_password_grant_token(
    *,
    issuer: str,
    client_id: str,
    username: str,
    password: str,
    client_secret: str | None = None,
) -> str:
    token_endpoint = f"{issuer.rstrip('/')}/protocol/openid-connect/token"
    form_data = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
        "scope": "openid profile email",
    }
    if client_secret:
        form_data["client_secret"] = client_secret

    async with httpx.AsyncClient(trust_env=False, timeout=30.0) as auth_client:
        response = await auth_client.post(token_endpoint, data=form_data)

    assert response.status_code == 200, response.text
    payload = response.json()
    access_token = payload.get("access_token")
    assert isinstance(access_token, str) and access_token
    return access_token


async def _query_liveness_aics(client: AsyncClient, access_token: str) -> set[str]:
    response = await client.post(
        f"{_HEARTBEAT_PREFIX}/liveness/query",
        headers=_auth_headers(access_token),
        json={"filter": {"conditions": [{"field": "silenceDurationSeconds", "op": "gte", "value": 0}]}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {item["data"]["aic"] for item in payload.get("items", [])}


async def _wait_for_visible_aics(
    client: AsyncClient,
    access_token: str,
    expected_aics: set[str],
    *,
    timeout_seconds: float = 15.0,
) -> set[str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        actual_aics = await _query_liveness_aics(client, access_token)
        if expected_aics.issubset(actual_aics):
            return actual_aics
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"等待心跳数据可见超时，期望至少看到 {expected_aics}，实际仅看到 {actual_aics}")
        await asyncio.sleep(0.5)


@pytest.fixture(scope="session")
def oidc_issuer() -> str:
    return _require_env("TEST_OIDC_ISSUER")


@pytest.fixture(scope="session")
def oidc_e2e_client_id() -> str:
    return _require_env("TEST_OIDC_E2E_CLIENT_ID")


@pytest.fixture(scope="session")
def oidc_e2e_client_secret() -> str | None:
    client_secret = os.getenv("TEST_OIDC_E2E_CLIENT_SECRET", "").strip()
    return client_secret or None


@pytest.fixture(scope="session")
def oidc_viewer_username() -> str:
    return _require_env("TEST_OIDC_VIEWER_USERNAME")


@pytest.fixture(scope="session")
def oidc_viewer_password() -> str:
    return _require_env("TEST_OIDC_VIEWER_PASSWORD")


@pytest.fixture(scope="session")
def oidc_operator_username() -> str:
    return _require_env("TEST_OIDC_OPERATOR_USERNAME")


@pytest.fixture(scope="session")
def oidc_operator_password() -> str:
    return _require_env("TEST_OIDC_OPERATOR_PASSWORD")


@pytest.fixture(scope="session")
def oidc_admin_username() -> str:
    return _require_env("TEST_OIDC_ADMIN_USERNAME")


@pytest.fixture(scope="session")
def oidc_admin_password() -> str:
    return _require_env("TEST_OIDC_ADMIN_PASSWORD")


@pytest.fixture(scope="session")
def foreign_oidc_issuer() -> str:
    return _require_env("TEST_OIDC_FOREIGN_ISSUER")


@pytest.fixture(scope="session")
def foreign_oidc_client_id() -> str:
    return _require_env("TEST_OIDC_FOREIGN_CLIENT_ID")


@pytest.fixture(scope="session")
def foreign_oidc_client_secret() -> str | None:
    client_secret = os.getenv("TEST_OIDC_FOREIGN_CLIENT_SECRET", "").strip()
    return client_secret or None


@pytest.fixture(scope="session")
def foreign_oidc_username() -> str:
    return _require_env("TEST_OIDC_FOREIGN_USERNAME")


@pytest.fixture(scope="session")
def foreign_oidc_password() -> str:
    return _require_env("TEST_OIDC_FOREIGN_PASSWORD")


@pytest.fixture
def issue_monitor_token(
    oidc_issuer: str,
    oidc_e2e_client_id: str,
    oidc_e2e_client_secret: str | None,
) -> Callable[[str, str], Awaitable[str]]:
    async def _issue(username: str, password: str) -> str:
        return await _issue_password_grant_token(
            issuer=oidc_issuer,
            client_id=oidc_e2e_client_id,
            client_secret=oidc_e2e_client_secret,
            username=username,
            password=password,
        )

    return _issue


@pytest.fixture
def issue_foreign_token(
    foreign_oidc_issuer: str,
    foreign_oidc_client_id: str,
    foreign_oidc_client_secret: str | None,
) -> Callable[[str, str], Awaitable[str]]:
    async def _issue(username: str, password: str) -> str:
        return await _issue_password_grant_token(
            issuer=foreign_oidc_issuer,
            client_id=foreign_oidc_client_id,
            client_secret=foreign_oidc_client_secret,
            username=username,
            password=password,
        )

    return _issue


async def test_oidc_viewer_is_limited_to_allowed_aic_scope(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
    issue_monitor_token: Callable[[str, str], Awaitable[str]],
    oidc_viewer_username: str,
    oidc_viewer_password: str,
    oidc_admin_username: str,
    oidc_admin_password: str,
) -> None:
    allowed_aic = "AIC-DEMO-001"
    foreign_aic = "AIC-FOREIGN-001"

    viewer_access_token = await issue_monitor_token(oidc_viewer_username, oidc_viewer_password)
    admin_access_token = await issue_monitor_token(oidc_admin_username, oidc_admin_password)

    await produce_heartbeat(allowed_aic)
    await produce_heartbeat(foreign_aic)

    admin_visible_aics = await _wait_for_visible_aics(
        e2e_http_client,
        admin_access_token,
        {allowed_aic, foreign_aic},
    )
    assert {allowed_aic, foreign_aic}.issubset(admin_visible_aics)

    allowed_response = await e2e_http_client.get(
        f"{_HEARTBEAT_PREFIX}/liveness/{allowed_aic}",
        headers=_auth_headers(viewer_access_token),
    )
    assert allowed_response.status_code == 200
    assert allowed_response.json()["data"]["aic"] == allowed_aic

    foreign_response = await e2e_http_client.get(
        f"{_HEARTBEAT_PREFIX}/liveness/{foreign_aic}",
        headers=_auth_headers(viewer_access_token),
    )
    assert foreign_response.status_code == 403
    assert foreign_response.json()["detail"] == "AIC is outside the principal scope"

    viewer_visible_aics = await _query_liveness_aics(e2e_http_client, viewer_access_token)
    assert viewer_visible_aics == {allowed_aic}


async def test_oidc_operator_endpoint_rejects_viewer_and_allows_operator(
    e2e_http_client: AsyncClient,
    issue_monitor_token: Callable[[str, str], Awaitable[str]],
    oidc_viewer_username: str,
    oidc_viewer_password: str,
    oidc_operator_username: str,
    oidc_operator_password: str,
) -> None:
    viewer_access_token = await issue_monitor_token(oidc_viewer_username, oidc_viewer_password)
    operator_access_token = await issue_monitor_token(oidc_operator_username, oidc_operator_password)

    viewer_response = await e2e_http_client.get(
        f"{_HEARTBEAT_PREFIX}/sync/info",
        headers=_auth_headers(viewer_access_token),
    )
    assert viewer_response.status_code == 403
    assert viewer_response.json()["detail"] == "Missing operator role"

    operator_response = await e2e_http_client.get(
        f"{_HEARTBEAT_PREFIX}/sync/info",
        headers=_auth_headers(operator_access_token),
    )
    assert operator_response.status_code == 200
    payload = operator_response.json()
    assert payload["type"] == "amp-alive-delta"
    assert payload["kafkaTopic"] == "amp.heartbeat.alive-delta"


async def test_cross_realm_token_is_rejected_by_monitor_api(
    e2e_http_client: AsyncClient,
    issue_foreign_token: Callable[[str, str], Awaitable[str]],
    foreign_oidc_username: str,
    foreign_oidc_password: str,
) -> None:
    foreign_access_token = await issue_foreign_token(foreign_oidc_username, foreign_oidc_password)

    response = await e2e_http_client.get(
        f"{_HEARTBEAT_PREFIX}/summary",
        headers=_auth_headers(foreign_access_token),
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
