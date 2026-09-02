"""黑盒 E2E：认证管理端人工审核主流程。"""

from __future__ import annotations

import pytest

from app.account.model import RoleType
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_object, response_json_string_map

pytestmark = pytest.mark.e2e


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _register_user(client, *, username: str, password: str, name: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "password": password,
            "name": name,
            "email": f"{username}@example.com",
        },
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def _read_current_user_id(client, *, access_token: str) -> str:
    response = await client.get("/api/v1/account/me", headers=_auth_headers(access_token))
    assert response.status_code == 200
    return str(response_json_object(response)["id"])


def _require_manual_review_status(response) -> dict[str, object]:
    assert response.status_code == 201
    payload = response_json_object(response)
    if payload["status"] == "APPROVED":
        pytest.skip("target server is running in auto-approve mode")
    assert payload["status"] == "PENDING"
    return payload


async def test_admin_can_review_identity_and_org_in_manual_mode(client, db_session, e2e_run_id: str) -> None:
    staff_username = f"verify-staff-{e2e_run_id}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="E2E Verification Staff",
    )
    await db_session.commit()

    client_tokens = await _register_user(
        client,
        username=f"verify-client-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="E2E Verification Client",
    )
    client_user_id = await _read_current_user_id(client, access_token=client_tokens["access_token"])
    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)

    identity_create = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    identity_payload = _require_manual_review_status(identity_create)

    identity_detail = await client.get(
        f"/api/v1/verification/admin/users/{client_user_id}/identity",
        headers=_auth_headers(staff_tokens["access_token"]),
    )
    assert identity_detail.status_code == 200
    assert identity_detail.json()["real_name"] == "Alice Zhang"

    identity_approve = await client.post(
        f"/api/v1/verification/admin/identity/{identity_payload['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "identity ok"},
    )
    assert identity_approve.status_code == 200
    assert identity_approve.json()["status"] == "APPROVED"

    org_create = await client.post(
        "/api/v1/verification/org",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "org_name": "ACPs Org",
            "usci": "91310000123456789X",
            "legal_rep_name": "Bob Li",
            "legal_rep_id_number": "310101199201019999",
        },
    )
    org_payload = _require_manual_review_status(org_create)

    org_detail = await client.get(
        f"/api/v1/verification/admin/users/{client_user_id}/org",
        headers=_auth_headers(staff_tokens["access_token"]),
    )
    assert org_detail.status_code == 200
    assert org_detail.json()["legal_rep_name"] == "Bob Li"

    org_approve = await client.post(
        f"/api/v1/verification/admin/org/{org_payload['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "org ok"},
    )
    assert org_approve.status_code == 200
    assert org_approve.json()["status"] == "APPROVED"
