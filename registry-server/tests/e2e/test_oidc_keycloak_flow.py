"""黑盒 E2E：registry-server 与 Keycloak 的真实 OIDC 联调。"""

import os
from collections.abc import Awaitable, Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.oidc]


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
def oidc_client_username() -> str:
    return _require_env("TEST_OIDC_CLIENT_USERNAME")


@pytest.fixture(scope="session")
def oidc_client_password() -> str:
    return _require_env("TEST_OIDC_CLIENT_PASSWORD")


@pytest.fixture(scope="session")
def oidc_admin_username() -> str:
    return _require_env("TEST_OIDC_ADMIN_USERNAME")


@pytest.fixture(scope="session")
def oidc_admin_password() -> str:
    return _require_env("TEST_OIDC_ADMIN_PASSWORD")


@pytest.fixture
def issue_token(
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


async def test_oidc_access_token_can_read_current_user_profile(
    client,
    issue_token: Callable[[str, str], Awaitable[str]],
    oidc_issuer: str,
    oidc_client_username: str,
    oidc_client_password: str,
) -> None:
    access_token = await issue_token(oidc_client_username, oidc_client_password)

    response = await client.get("/api/v1/account/me", headers=_auth_headers(access_token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["username"] == oidc_client_username
    assert payload["roles"] == ["CLIENT"]
    assert payload["is_active"] is True
    assert payload["external_principal"]["provider"] == "oidc"
    assert payload["external_principal"]["issuer"] == oidc_issuer
    assert payload["external_principal"]["username"] == oidc_client_username
    assert payload["external_principal"]["principal_id"]


async def test_oidc_role_mapping_enforces_admin_boundaries(
    client,
    issue_token: Callable[[str, str], Awaitable[str]],
    oidc_client_username: str,
    oidc_client_password: str,
    oidc_admin_username: str,
    oidc_admin_password: str,
) -> None:
    client_access_token = await issue_token(oidc_client_username, oidc_client_password)
    admin_access_token = await issue_token(oidc_admin_username, oidc_admin_password)

    forbidden_response = await client.get("/api/v1/account/user", headers=_auth_headers(client_access_token))
    assert forbidden_response.status_code == 403

    admin_response = await client.get(
        "/api/v1/account/user",
        headers=_auth_headers(admin_access_token),
        params={"page": 1, "page_size": 20},
    )
    assert admin_response.status_code == 200
    payload = admin_response.json()
    assert payload["total"] >= 1
    assert any(item["username"] == oidc_admin_username for item in payload["items"])


async def test_oidc_logout_returns_keycloak_end_session_endpoint(
    client,
    issue_token: Callable[[str, str], Awaitable[str]],
    oidc_issuer: str,
    oidc_client_username: str,
    oidc_client_password: str,
) -> None:
    access_token = await issue_token(oidc_client_username, oidc_client_password)

    response = await client.post("/api/v1/auth/logout", headers=_auth_headers(access_token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["end_session_endpoint"] == f"{oidc_issuer.rstrip('/')}/protocol/openid-connect/logout"
