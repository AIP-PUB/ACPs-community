"""黑盒 E2E：Agent 主生命周期。"""

import pytest

from app.account.model import RoleType
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_string_map

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


async def test_agent_lifecycle_create_submit_review_and_publish(client, db_session, e2e_run_id: str) -> None:
    staff_username = f"staff-{e2e_run_id}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="E2E Staff",
    )
    await db_session.commit()

    client_username = f"agent-owner-{e2e_run_id}"
    client_tokens = await _register_user(
        client,
        username=client_username,
        password=DEFAULT_LOGIN_VALUE,
        name="E2E Agent Owner",
    )

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "name": f"E2E Agent {e2e_run_id}",
            "version": "1.0.0",
            "description": "created in e2e",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    agent_id = created["id"]
    assert created["approval_status"] == "DRAFT"

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert submit_response.status_code == 200
    assert submit_response.json()["approval_status"] == "PENDING"

    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)
    process_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"approve": True, "comments": "approved in e2e"},
    )
    assert process_response.status_code == 200
    processed = process_response.json()
    assert processed["approval_status"] == "APPROVED"
    assert processed["aic"]
    aic_parts = processed["aic"].split(".")
    assert aic_parts[5] == "1"
    assert len(aic_parts[6]) >= 1

    public_response = await client.get(f"/api/v1/agent/public/{agent_id}")
    assert public_response.status_code == 200
    public_payload = public_response.json()
    assert public_payload["id"] == agent_id
    assert public_payload["approval_status"] == "APPROVED"
    assert public_payload["aic"] == processed["aic"]


async def test_non_owner_cannot_read_pending_agent_from_client_api(client, e2e_run_id: str) -> None:
    owner_tokens = await _register_user(
        client,
        username=f"owner-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="Owner",
    )
    visitor_tokens = await _register_user(
        client,
        username=f"visitor-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="Visitor",
    )

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={"name": f"Private Agent {e2e_run_id}", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert submit_response.status_code == 200

    forbidden_response = await client.get(
        f"/api/v1/agent/client/{agent_id}",
        headers=_auth_headers(visitor_tokens["access_token"]),
    )
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["error_name"] == "ACCESS_DENIED_NOT_OWNER"
