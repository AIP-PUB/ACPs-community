"""真实数据库集成测试：account self-service 路由。"""

from __future__ import annotations

import uuid

import pytest

from tests.support.constants import DEFAULT_LOGIN_VALUE, ROTATED_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.integration


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def test_current_user_can_update_profile_and_password(client, db_session) -> None:
    username = f"self-{uuid.uuid4().hex[:8]}"
    user = await create_user(
        db_session,
        username=username,
        password=DEFAULT_LOGIN_VALUE,
        email=f"{username}@example.com",
        name="Self User",
    )
    await db_session.commit()

    tokens = await _login(client, username=user.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(tokens["access_token"])

    update_response = await client.put(
        "/api/v1/account/me",
        headers=headers,
        json={
            "name": "Updated Self User",
            "org_name": "ACPs Labs",
            "org_code": "ACPs-001",
            "org_address": "Beijing",
        },
    )
    assert update_response.status_code == 200
    updated_profile = update_response.json()
    assert updated_profile["name"] == "Updated Self User"
    assert updated_profile["org_name"] == "ACPs Labs"
    assert updated_profile["org_code"] == "ACPs-001"
    assert updated_profile["org_address"] == "Beijing"
    assert updated_profile.get("aic_provider_code") is None

    ignored_code_response = await client.put(
        "/api/v1/account/me",
        headers=headers,
        json={"name": "Updated Self User", "aic_provider_code": "34C2"},
    )
    assert ignored_code_response.status_code == 200
    assert ignored_code_response.json().get("aic_provider_code") is None

    password_response = await client.put(
        "/api/v1/account/me/password",
        headers=headers,
        json={"old_password": DEFAULT_LOGIN_VALUE, "new_password": ROTATED_LOGIN_VALUE},
    )
    assert password_response.status_code == 200
    assert password_response.json() == {"success": True, "message": "Password updated successfully"}

    old_password_login = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": DEFAULT_LOGIN_VALUE},
    )
    assert old_password_login.status_code == 401
    assert old_password_login.json()["error_name"] == "INVALID_CREDENTIALS"

    new_password_login = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": ROTATED_LOGIN_VALUE},
    )
    assert new_password_login.status_code == 200
