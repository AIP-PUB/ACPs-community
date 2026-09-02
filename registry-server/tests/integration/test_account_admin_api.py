"""真实数据库集成测试：account/admin 管理流。"""

import uuid

import pytest

from app.account.model import RoleType, User
from tests.support.constants import DEFAULT_LOGIN_VALUE, ROTATED_LOGIN_VALUE
from tests.support.database import create_user, ensure_role
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


async def test_admin_can_manage_users_and_roles(client, db_session) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.STAFF)
    await ensure_role(db_session, RoleType.CLIENT)
    admin = await create_user(
        db_session,
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="Account Admin",
    )
    await db_session.commit()

    admin_tokens = await _login(client, username=admin.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(admin_tokens["access_token"])

    create_response = await client.post(
        "/api/v1/account/user",
        headers=headers,
        json={
            "username": f"managed-{uuid.uuid4().hex[:8]}",
            "password": DEFAULT_LOGIN_VALUE,
            "name": "Managed User",
            "email": f"managed-{uuid.uuid4().hex[:8]}@example.com",
            "roles": ["STAFF"],
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    user_id = created["id"]
    username = created["username"]
    assert created["roles"] == ["STAFF"]

    read_response = await client.get(f"/api/v1/account/user/{user_id}", headers=headers)
    assert read_response.status_code == 200
    assert read_response.json()["username"] == username

    update_response = await client.put(
        f"/api/v1/account/user/{user_id}",
        headers=headers,
        json={"name": "Updated Managed User", "org_name": "ACPs Org"},
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["name"] == "Updated Managed User"
    assert updated["org_name"] == "ACPs Org"

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
    assert reset_password_response.json()["message"] == "Password reset successfully"

    managed_login = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": ROTATED_LOGIN_VALUE},
    )
    assert managed_login.status_code == 200

    role_list_response = await client.get("/api/v1/account/role", headers=headers)
    assert role_list_response.status_code == 200
    role_names = {item["name"] for item in role_list_response.json()}
    assert {"ADMIN", "STAFF", "CLIENT"}.issubset(role_names)

    inactive_list_response = await client.get(
        "/api/v1/account/user",
        headers=headers,
        params={"username": username, "is_active": "true"},
    )
    assert inactive_list_response.status_code == 200
    assert inactive_list_response.json()["total"] == 1

    delete_response = await client.delete(f"/api/v1/account/user/{user_id}", headers=headers)
    assert delete_response.status_code == 200
    assert delete_response.json()["success"] is True

    inactive_filter_response = await client.get(
        "/api/v1/account/user",
        headers=headers,
        params={"username": username, "is_active": "false"},
    )
    assert inactive_filter_response.status_code == 200
    assert inactive_filter_response.json()["total"] == 1
    assert inactive_filter_response.json()["items"][0]["is_active"] is False

    failed_login_response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": ROTATED_LOGIN_VALUE},
    )
    assert failed_login_response.status_code == 401
    assert failed_login_response.json()["error_name"] == "INVALID_CREDENTIALS"


async def test_admin_reset_password_revokes_target_users_active_session(client, db_session) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.CLIENT)
    admin = await create_user(
        db_session,
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="Reset Admin",
    )
    target = await create_user(
        db_session,
        username=f"target-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
        name="Reset Target",
    )
    await db_session.commit()

    admin_tokens = await _login(client, username=admin.username or "", password=DEFAULT_LOGIN_VALUE)
    target_tokens = await _login(client, username=target.username or "", password=DEFAULT_LOGIN_VALUE)
    admin_headers = _auth_headers(admin_tokens["access_token"])
    target_headers = _auth_headers(target_tokens["access_token"])

    reset_password_response = await client.put(
        f"/api/v1/account/user/{target.id}/password",
        headers=admin_headers,
        json={"new_password": ROTATED_LOGIN_VALUE},
    )
    assert reset_password_response.status_code == 200
    assert reset_password_response.json()["message"] == "Password reset successfully"

    stale_session_response = await client.get("/api/v1/account/me", headers=target_headers)
    assert stale_session_response.status_code == 401
    assert stale_session_response.json()["error_name"] == "TOKEN_VALIDATION_ERROR"

    new_login_response = await client.post(
        "/api/v1/auth/login",
        data={"username": target.username, "password": ROTATED_LOGIN_VALUE},
    )
    assert new_login_response.status_code == 200


async def test_staff_can_read_single_user_and_list_users_but_not_admin_manage_users(client, db_session) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.STAFF)
    await ensure_role(db_session, RoleType.CLIENT)
    staff = await create_user(
        db_session,
        username=f"staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Staff Reader",
    )
    target_user = await create_user(
        db_session,
        username=f"target-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
        name="Target User",
    )
    await db_session.commit()

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(staff_tokens["access_token"])

    read_response = await client.get(f"/api/v1/account/user/{target_user.id}", headers=headers)
    assert read_response.status_code == 200
    assert read_response.json()["username"] == target_user.username

    list_response = await client.get("/api/v1/account/user", headers=headers)
    assert list_response.status_code == 200
    assert any(item["id"] == str(target_user.id) for item in list_response.json()["items"])

    create_response = await client.post(
        "/api/v1/account/user",
        headers=headers,
        json={
            "username": f"forbidden-{uuid.uuid4().hex[:8]}",
            "password": DEFAULT_LOGIN_VALUE,
            "name": "Forbidden",
            "roles": ["CLIENT"],
        },
    )
    assert create_response.status_code == 403


async def test_admin_user_list_accepts_page_and_marks_page_num_deprecated(client, db_session) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.CLIENT)
    admin = await create_user(
        db_session,
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="Pagination Admin",
    )
    await create_user(
        db_session,
        username=f"listed-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
        name="Listed User",
    )
    await db_session.commit()

    admin_tokens = await _login(client, username=admin.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(admin_tokens["access_token"])

    page_response = await client.get(
        "/api/v1/account/user",
        headers=headers,
        params={"page": 1, "page_size": 1},
    )
    assert page_response.status_code == 200
    page_payload = page_response.json()
    assert page_payload["page"] == 1
    assert page_payload["page_num"] == 1
    assert page_payload["page_size"] == 1
    assert "Deprecation" not in page_response.headers

    legacy_page_response = await client.get(
        "/api/v1/account/user",
        headers=headers,
        params={"page_num": 1, "page_size": 1},
    )
    assert legacy_page_response.status_code == 200
    legacy_payload = legacy_page_response.json()
    assert legacy_payload["page"] == 1
    assert legacy_payload["page_num"] == 1
    assert legacy_page_response.headers["Deprecation"] == "true"
    assert "page_num query parameter is deprecated" in legacy_page_response.headers["Warning"]


async def test_admin_can_batch_delete_users_and_reports_missing_ids(client, db_session) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.CLIENT)
    admin = await create_user(
        db_session,
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="Batch Delete Admin",
    )
    target_one = await create_user(
        db_session,
        username=f"batch-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
        name="Batch Delete Target One",
    )
    target_two = await create_user(
        db_session,
        username=f"batch-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
        name="Batch Delete Target Two",
    )
    await db_session.commit()

    admin_tokens = await _login(client, username=admin.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(admin_tokens["access_token"])
    missing_user_id = uuid.uuid4()
    target_one_id = target_one.id
    target_two_id = target_two.id

    response = await client.request(
        "DELETE",
        "/api/v1/account/user",
        headers=headers,
        json=[str(target_one_id), str(target_two_id), str(missing_user_id)],
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["success"]) == {str(target_one_id), str(target_two_id)}
    assert payload["failed"] == [{"id": str(missing_user_id), "reason": "User not found"}]

    db_session.expire_all()
    refreshed_one = await db_session.get(User, target_one_id)
    refreshed_two = await db_session.get(User, target_two_id)
    assert refreshed_one is not None
    assert refreshed_two is not None
    assert refreshed_one.is_active is False
    assert refreshed_two.is_active is False


async def test_staff_can_set_aic_provider_code_and_client_cannot(client, db_session) -> None:
    await ensure_role(db_session, RoleType.ADMIN)
    await ensure_role(db_session, RoleType.STAFF)
    await ensure_role(db_session, RoleType.CLIENT)
    staff = await create_user(
        db_session,
        username=f"staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    client_user = await create_user(
        db_session,
        username=f"client-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
    )
    target = await create_user(
        db_session,
        username=f"vendor-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
    )
    await db_session.commit()

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_headers = _auth_headers(staff_tokens["access_token"])
    set_response = await client.put(
        f"/api/v1/account/user/{target.id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "34c2"},
    )
    assert set_response.status_code == 200
    assert set_response.json()["aic_provider_code"] == "34C2"

    idempotent_response = await client.put(
        f"/api/v1/account/user/{target.id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "34C2"},
    )
    assert idempotent_response.status_code == 200

    zero_response = await client.put(
        f"/api/v1/account/user/{target.id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "0"},
    )
    assert zero_response.status_code == 422

    client_tokens = await _login(client, username=client_user.username or "", password=DEFAULT_LOGIN_VALUE)
    client_headers = _auth_headers(client_tokens["access_token"])
    forbidden_response = await client.put(
        f"/api/v1/account/user/{target.id}/aic-provider-code",
        headers=client_headers,
        json={"aic_provider_code": "AAAAAA"},
    )
    assert forbidden_response.status_code == 403

    missing_response = await client.put(
        f"/api/v1/account/user/{uuid.uuid4()}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "AAAAAA"},
    )
    assert missing_response.status_code == 404


async def test_staff_cannot_assign_duplicate_aic_provider_code(client, db_session) -> None:
    await ensure_role(db_session, RoleType.STAFF)
    await ensure_role(db_session, RoleType.CLIENT)
    staff = await create_user(
        db_session,
        username=f"staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    first = await create_user(
        db_session,
        username=f"vendor-a-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
    )
    second = await create_user(
        db_session,
        username=f"vendor-b-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.CLIENT,),
    )
    await db_session.commit()

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_headers = _auth_headers(staff_tokens["access_token"])
    first_response = await client.put(
        f"/api/v1/account/user/{first.id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "DUP001"},
    )
    assert first_response.status_code == 200
    assert first_response.json()["aic_provider_code"] == "DUP001"

    conflict_response = await client.put(
        f"/api/v1/account/user/{second.id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "dup001"},
    )
    assert conflict_response.status_code == 409
    assert conflict_response.json()["error_name"] == "AIC_PROVIDER_CODE_CONFLICT"
