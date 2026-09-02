"""黑盒 E2E：管理员用户与角色管理流。"""

import pytest

from app.account.model import RoleType
from tests.support.constants import DEFAULT_LOGIN_VALUE, ROTATED_LOGIN_VALUE
from tests.support.database import create_user, ensure_role
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.e2e


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def test_admin_user_management_lifecycle(client, db_session, e2e_run_id: str) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.STAFF)
    await ensure_role(db_session, RoleType.CLIENT)
    admin = await create_user(
        db_session,
        username=f"admin-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="E2E Admin",
    )
    await db_session.commit()

    admin_tokens = await _login(client, username=admin.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(admin_tokens["access_token"])
    managed_username = f"managed-{e2e_run_id}"

    role_list_response = await client.get("/api/v1/account/role", headers=headers)
    assert role_list_response.status_code == 200
    role_names = {item["name"] for item in role_list_response.json()}
    assert {"ADMIN", "STAFF", "CLIENT"}.issubset(role_names)

    create_response = await client.post(
        "/api/v1/account/user",
        headers=headers,
        json={
            "username": managed_username,
            "password": DEFAULT_LOGIN_VALUE,
            "name": "Managed User",
            "email": f"{managed_username}@example.com",
            "roles": ["STAFF"],
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    user_id = created["id"]
    assert created["roles"] == ["STAFF"]

    update_response = await client.put(
        f"/api/v1/account/user/{user_id}",
        headers=headers,
        json={"name": "Updated Managed User", "org_name": "ACPs Admin Org"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Updated Managed User"
    assert update_response.json()["org_name"] == "ACPs Admin Org"

    role_update_response = await client.put(
        f"/api/v1/account/user/{user_id}/roles",
        headers=headers,
        json={"role_names": ["CLIENT"]},
    )
    assert role_update_response.status_code == 200
    assert role_update_response.json()["roles"] == ["CLIENT"]

    reset_password_response = await client.put(
        f"/api/v1/account/user/{user_id}/password",
        headers=headers,
        json={"new_password": ROTATED_LOGIN_VALUE},
    )
    assert reset_password_response.status_code == 200

    managed_tokens = await _login(client, username=managed_username, password=ROTATED_LOGIN_VALUE)
    me_response = await client.get("/api/v1/account/me", headers=_auth_headers(managed_tokens["access_token"]))
    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["username"] == managed_username
    assert me_payload["roles"] == ["CLIENT"]

    delete_response = await client.delete(f"/api/v1/account/user/{user_id}", headers=headers)
    assert delete_response.status_code == 200
    assert delete_response.json()["success"] is True

    failed_login = await client.post(
        "/api/v1/auth/login",
        data={"username": managed_username, "password": ROTATED_LOGIN_VALUE},
    )
    assert failed_login.status_code == 401
    assert failed_login.json()["error_name"] == "INVALID_CREDENTIALS"
