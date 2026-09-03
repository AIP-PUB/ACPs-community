"""真实数据库集成测试：Agent 生命周期与权限边界。"""

import uuid

import pytest

from app.account.model import RoleType
from app.agent import api as agent_api_module
from app.agent.model import Agent, ApprovalStatus
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.integration


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _register_client(client, *, username: str, password: str, name: str) -> dict[str, str]:
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


async def _create_agent_draft(client, *, access_token: str, name: str, version: str = "1.0.0") -> dict[str, str]:
    response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(access_token),
        json={"name": name, "version": version},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def test_agent_lifecycle_create_submit_approve_and_public_read(client, db_session, monkeypatch) -> None:
    captured_webhook_types: list[list[str]] = []

    def fake_trigger_data_change_webhook(_sync_session, data_types: list[str]) -> None:
        captured_webhook_types.append(list(data_types))

    monkeypatch.setattr(agent_api_module, "trigger_data_change_webhook", fake_trigger_data_change_webhook)

    staff_username = f"staff-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Workflow Staff",
    )
    await db_session.commit()

    client_username = f"client-{uuid.uuid4().hex[:8]}"
    client_tokens = await _register_client(
        client,
        username=client_username,
        password=DEFAULT_LOGIN_VALUE,
        name="Workflow Client",
    )

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "name": "Workflow Agent",
            "version": "1.0.0",
            "description": "draft agent",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    agent_id = created["id"]
    assert created["approval_status"] == "DRAFT"

    update_response = await client.put(
        f"/api/v1/agent/client/{agent_id}",
        headers=_auth_headers(client_tokens["access_token"]),
        json={"description": "updated before submit"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["description"] == "updated before submit"

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
        json={"approve": True, "comments": "looks good"},
    )
    assert process_response.status_code == 200
    processed = process_response.json()
    assert processed["approval_status"] == "APPROVED"
    assert processed["aic"]
    aic_parts = processed["aic"].split(".")
    assert aic_parts[5] == "1"
    assert captured_webhook_types == [["acs"]]

    db_session.expire_all()
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    assert agent is not None
    assert agent.approval_status == ApprovalStatus.APPROVED
    assert agent.aic == processed["aic"]

    public_response = await client.get(f"/api/v1/agent/public/{agent_id}")
    assert public_response.status_code == 200
    public_payload = public_response.json()
    assert public_payload["id"] == agent_id
    assert public_payload["aic"] == processed["aic"]
    assert public_payload["approval_status"] == "APPROVED"


async def test_public_recent_returns_only_approved_agents_with_user_data(client, db_session) -> None:
    staff_username = f"recent-staff-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Recent Staff",
    )
    await db_session.commit()

    approved_client_username = f"approved-{uuid.uuid4().hex[:8]}"
    approved_client_tokens = await _register_client(
        client,
        username=approved_client_username,
        password=DEFAULT_LOGIN_VALUE,
        name="Approved Client",
    )
    approved_agent = await _create_agent_draft(
        client,
        access_token=approved_client_tokens["access_token"],
        name=f"Approved Agent {uuid.uuid4().hex[:6]}",
    )
    approved_agent_id = approved_agent["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{approved_agent_id}/submit",
        headers=_auth_headers(approved_client_tokens["access_token"]),
    )
    assert submit_response.status_code == 200

    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)
    process_response = await client.post(
        f"/api/v1/agent/staff/{approved_agent_id}/process",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"approve": True, "comments": "publish it"},
    )
    assert process_response.status_code == 200

    draft_client_tokens = await _register_client(
        client,
        username=f"draft-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Draft Client",
    )
    draft_create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(draft_client_tokens["access_token"]),
        json={"name": f"Draft Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert draft_create_response.status_code == 200

    recent_response = await client.get("/api/v1/agent/public/recent", params={"limit": 5, "with_users": True})
    assert recent_response.status_code == 200
    payload = recent_response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == approved_agent_id
    assert payload["items"][0]["created_by"]["username"] == approved_client_username
    assert payload["items"][0]["approval_status"] == "APPROVED"


async def test_public_acs_example_returns_jsonc_sample(client) -> None:
    response = await client.get("/api/v1/agent/public/acs_example")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, str)
    assert "securitySchemes" in payload
    assert "skills" in payload


async def test_client_list_returns_only_current_users_agents(client) -> None:
    first_username = f"client-{uuid.uuid4().hex[:8]}"
    first_client_tokens = await _register_client(
        client,
        username=first_username,
        password=DEFAULT_LOGIN_VALUE,
        name="First Client",
    )
    second_client_tokens = await _register_client(
        client,
        username=f"client-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Second Client",
    )

    first_agent_one = await _create_agent_draft(
        client,
        access_token=first_client_tokens["access_token"],
        name=f"List Agent {uuid.uuid4().hex[:6]}",
        version="1.0.0",
    )
    first_agent_two = await _create_agent_draft(
        client,
        access_token=first_client_tokens["access_token"],
        name=f"List Agent {uuid.uuid4().hex[:6]}",
        version="2.0.0",
    )
    other_agent_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(second_client_tokens["access_token"]),
        json={"name": f"Other Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert other_agent_response.status_code == 200

    list_response = await client.get(
        "/api/v1/agent/client",
        headers=_auth_headers(first_client_tokens["access_token"]),
        params={"page_size": 10, "with_users": True},
    )
    assert list_response.status_code == 200
    payload = list_response.json()
    assert payload["total"] == 2
    assert {item["id"] for item in payload["items"]} == {first_agent_one["id"], first_agent_two["id"]}
    assert {item["created_by"]["username"] for item in payload["items"]} == {first_username}


async def test_client_cannot_read_other_users_non_approved_agent(client) -> None:
    owner_tokens = await _register_client(
        client,
        username=f"owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Owner",
    )
    visitor_tokens = await _register_client(
        client,
        username=f"visitor-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Visitor",
    )

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={"name": "Private Draft Agent", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]

    read_response = await client.get(
        f"/api/v1/agent/client/{agent_id}",
        headers=_auth_headers(visitor_tokens["access_token"]),
    )
    assert read_response.status_code == 403
    assert read_response.json()["error_name"] == "ACCESS_DENIED_NOT_OWNER"


async def test_admin_can_process_disable_and_enable_agents(client, db_session) -> None:
    admin_username = f"admin-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=admin_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="Admin Reviewer",
    )
    await db_session.commit()

    client_tokens = await _register_client(
        client,
        username=f"submitter-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Submitter",
    )
    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(client_tokens["access_token"]),
        json={"name": "Admin Managed Agent", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert submit_response.status_code == 200
    assert submit_response.json()["approval_status"] == "PENDING"

    admin_tokens = await _login(client, username=admin_username, password=DEFAULT_LOGIN_VALUE)
    process_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(admin_tokens["access_token"]),
        json={"approve": True, "comments": "approved by admin"},
    )
    assert process_response.status_code == 200
    assert process_response.json()["approval_status"] == "APPROVED"

    disable_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/disable",
        headers=_auth_headers(admin_tokens["access_token"]),
        json="admin freeze",
    )
    assert disable_response.status_code == 200
    disabled = disable_response.json()
    assert disabled["is_active"] is False
    assert disabled["is_disabled"] is True

    enable_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/enable",
        headers=_auth_headers(admin_tokens["access_token"]),
    )
    assert enable_response.status_code == 200
    enabled = enable_response.json()
    assert enabled["is_active"] is True
    assert enabled["is_disabled"] is False


async def test_rejected_agent_resubmission_clears_previous_review_metadata(client, db_session) -> None:
    staff_username = f"reviewer-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Review Staff",
    )
    await db_session.commit()

    client_tokens = await _register_client(
        client,
        username=f"resubmit-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Resubmit Client",
    )
    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(client_tokens["access_token"]),
        json={"name": f"Resubmit Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert submit_response.status_code == 200

    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)
    reject_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"approve": False, "comments": "needs changes"},
    )
    assert reject_response.status_code == 200
    rejected = reject_response.json()
    assert rejected["approval_status"] == "REJECTED"
    assert rejected["processed_by_id"]
    assert rejected["processed_at"] is not None
    assert rejected["process_comments"] == "needs changes"

    resubmit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert resubmit_response.status_code == 200
    resubmitted = resubmit_response.json()
    assert resubmitted["approval_status"] == "PENDING"
    assert resubmitted["submitted_at"] is not None
    assert resubmitted["processed_by_id"] is None
    assert resubmitted["processed_at"] is None
    assert resubmitted["process_comments"] is None

    db_session.expire_all()
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    assert agent is not None
    assert agent.approval_status == ApprovalStatus.PENDING
    assert agent.processed_by_id is None
    assert agent.processed_at is None
    assert agent.process_comments is None


async def test_deleted_agent_cannot_be_resubmitted_or_processed(client, db_session) -> None:
    staff_username = f"delete-reviewer-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Delete Reviewer",
    )
    await db_session.commit()

    client_tokens = await _register_client(
        client,
        username=f"deleted-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Deleted Client",
    )
    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(client_tokens["access_token"]),
        json={"name": f"Deleted Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert submit_response.status_code == 200

    delete_response = await client.request(
        "DELETE",
        f"/api/v1/agent/client/{agent_id}",
        headers=_auth_headers(client_tokens["access_token"]),
        json="cleanup",
    )
    assert delete_response.status_code == 200

    resubmit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert resubmit_response.status_code == 400
    assert resubmit_response.json()["error_name"] == "INVALID_STATUS_TRANSITION"

    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)
    process_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"approve": True, "comments": "should fail"},
    )
    assert process_response.status_code == 400
    assert process_response.json()["error_name"] == "INVALID_STATUS_TRANSITION"


async def test_disabled_pending_agent_cannot_be_canceled_or_processed(client, db_session) -> None:
    admin_username = f"freeze-admin-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=admin_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.ADMIN,),
        name="Freeze Admin",
    )
    await db_session.commit()

    client_tokens = await _register_client(
        client,
        username=f"frozen-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Frozen Client",
    )
    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(client_tokens["access_token"]),
        json={"name": f"Frozen Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert submit_response.status_code == 200

    admin_tokens = await _login(client, username=admin_username, password=DEFAULT_LOGIN_VALUE)
    disable_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/disable",
        headers=_auth_headers(admin_tokens["access_token"]),
        json="freeze pending",
    )
    assert disable_response.status_code == 200
    assert disable_response.json()["is_disabled"] is True

    cancel_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/cancel",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert cancel_response.status_code == 400
    assert cancel_response.json()["error_name"] == "INVALID_STATUS_TRANSITION"

    process_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(admin_tokens["access_token"]),
        json={"approve": True, "comments": "should fail"},
    )
    assert process_response.status_code == 400
    assert process_response.json()["error_name"] == "INVALID_STATUS_TRANSITION"
