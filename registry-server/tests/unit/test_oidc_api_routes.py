from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.account.model import Role, RoleType, User
from app.core import auth as auth_module
from app.core.config import settings

pytestmark = pytest.mark.unit


def _make_admin_user() -> User:
    user = User(username="admin-user", email="admin@example.com", hashed_password="stored-hash")
    user.roles = [Role(name=RoleType.ADMIN)]
    return user


@pytest.fixture
def oidc_enabled_config() -> Generator[None]:
    original = deepcopy(settings._toml)
    settings._toml.setdefault("oidc", {}).update(
        {
            "enabled": True,
            "issuer": "https://issuer.example/realms/acps-registry",
            "audience": "registry-api",
            "allowed_azp": ["registry-web"],
            "client_id": "registry-api",
            "algorithms": ["EdDSA"],
            "role_source_client_id": "registry-api",
        }
    )
    yield
    app_main.app.dependency_overrides.clear()
    object.__setattr__(settings, "_toml", original)


def test_local_auth_endpoints_return_410_when_oidc_enabled(oidc_enabled_config: None) -> None:
    with TestClient(app_main.app) as client:
        login_response = client.post(
            "/api/v1/auth/login",
            data={"username": "demo", "password": "secret"},
        )
        register_response = client.post(
            "/api/v1/auth/register",
            json={"username": "demo", "password": "secret"},
        )
        refresh_response = client.post(
            "/api/v1/auth/refresh-token",
            json={"refresh_token": "dummy"},
        )

    assert login_response.status_code == 410
    assert register_response.status_code == 410
    assert refresh_response.status_code == 410


def test_account_local_auth_endpoints_return_410_when_oidc_enabled(
    oidc_enabled_config: None,
) -> None:
    current_user = _make_admin_user()

    async def _override_current_user() -> object:
        return current_user

    app_main.app.dependency_overrides[auth_module.get_current_user] = _override_current_user

    with TestClient(app_main.app) as client:
        password_by_code_response = client.post(
            "/api/v1/account/update_password",
            json={"email": "demo@example.com", "code": "123456", "password": "Str0ngP@ss!"},
        )
        current_password_response = client.put(
            "/api/v1/account/me/password",
            json={"old_password": "old-pass", "new_password": "Str0ngP@ss!"},
        )

    assert password_by_code_response.status_code == 410
    assert current_password_response.status_code == 410


def test_admin_local_account_management_endpoints_return_410_when_oidc_enabled(
    oidc_enabled_config: None,
) -> None:
    current_user = _make_admin_user()

    async def _override_current_user() -> object:
        return current_user

    app_main.app.dependency_overrides[auth_module.get_current_user] = _override_current_user

    with TestClient(app_main.app) as client:
        create_user_response = client.post(
            "/api/v1/account/user",
            json={"username": "demo", "password": "Str0ngP@ss!", "roles": ["CLIENT"]},
        )
        reset_password_mail_response = client.put(
            f"/api/v1/account/user/{current_user.id}/reset_password",
            json={"current_password": "old-pass"},
        )
        reset_password_response = client.put(
            f"/api/v1/account/user/{current_user.id}/password",
            json={"new_password": "Str0ngP@ss!"},
        )

    assert create_user_response.status_code == 410
    assert reset_password_mail_response.status_code == 410
    assert reset_password_response.status_code == 410


def test_admin_reset_password_requires_current_password(monkeypatch: pytest.MonkeyPatch) -> None:
    original = deepcopy(settings._toml)
    settings._toml.setdefault("oidc", {})["enabled"] = False
    current_user = _make_admin_user()

    async def _override_current_user() -> object:
        return current_user

    app_main.app.dependency_overrides[auth_module.get_current_user] = _override_current_user
    monkeypatch.setattr("app.account.api_account.verify_password", lambda plain, hashed: (False, False))

    try:
        with TestClient(app_main.app) as client:
            response = client.put(
                f"/api/v1/account/user/{current_user.id}/reset_password",
                json={"current_password": "wrong-pass"},
            )
    finally:
        app_main.app.dependency_overrides.clear()
        object.__setattr__(settings, "_toml", original)

    assert response.status_code == 200
    assert response.json() == {"success": False, "message": "密码错误"}


def test_logout_returns_keycloak_end_session_endpoint_when_oidc_enabled(oidc_enabled_config: None) -> None:
    with TestClient(app_main.app) as client:
        response = client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["end_session_endpoint"].endswith("/protocol/openid-connect/logout")


def test_account_me_returns_external_principal_summary_without_raw_fields(oidc_enabled_config: None) -> None:
    user = User(username="shadow-user", email="shadow@example.com")
    user.roles = [Role(name=RoleType.CLIENT)]
    user.auth_provider = "oidc"
    user.external_issuer = "https://issuer.example/realms/acps-registry"
    user.external_subject = "raw-subject"
    user.external_principal_id = "principal-id"
    user.external_username = "shadow-user"

    async def _override_current_user() -> object:
        return user

    app_main.app.dependency_overrides[auth_module.get_current_user] = _override_current_user

    with TestClient(app_main.app) as client:
        response = client.get("/api/v1/account/me")

    assert response.status_code == 200
    payload = response.json()
    assert payload["external_principal"]["provider"] == "oidc"
    assert payload["external_principal"]["issuer"] == "https://issuer.example/realms/acps-registry"
    assert payload["external_principal"]["principal_id"] == "principal-id"
    assert "external_subject" not in payload
    assert "subject" not in payload
    assert "principal_key" not in payload
    assert "raw_claims" not in payload
