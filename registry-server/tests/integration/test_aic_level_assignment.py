"""集成测试：AIC 第6/7 级按配置与账户赋值。"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy import select

from app.account.model import RoleType, User
from app.agent.model import Agent
from app.core.config import settings
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


async def _submit_and_approve(
    client,
    *,
    owner_token: str,
    staff_token: str,
    name: str,
) -> str:
    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(owner_token),
        json={"name": name, "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]
    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(owner_token),
    )
    assert submit_response.status_code == 200
    process_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(staff_token),
        json={"approve": True, "comments": "ok"},
    )
    assert process_response.status_code == 200
    aic = process_response.json()["aic"]
    assert isinstance(aic, str)
    return aic


async def _create_staff_and_owner(
    client,
    db_session,
    *,
    prefix: str,
) -> tuple[dict[str, str], dict[str, str], str]:
    staff_username = f"{prefix}-staff-{uuid.uuid4().hex[:8]}"
    staff = await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    await db_session.commit()
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    owner_username = f"{prefix}-owner-{uuid.uuid4().hex[:8]}"
    owner_tokens = await _register_client(
        client,
        username=owner_username,
        password=DEFAULT_LOGIN_VALUE,
        name=f"{prefix} Owner",
    )
    me_response = await client.get("/api/v1/account/me", headers=_auth_headers(owner_tokens["access_token"]))
    owner_id = me_response.json()["id"]
    assert isinstance(owner_id, str)
    return staff_tokens, owner_tokens, owner_id


async def test_two_accounts_share_arsp_and_get_distinct_vendor_codes(client, db_session) -> None:
    staff_username = f"aic-staff-{uuid.uuid4().hex[:8]}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    await db_session.commit()
    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)

    first_username = f"vendor-a-{uuid.uuid4().hex[:8]}"
    second_username = f"vendor-b-{uuid.uuid4().hex[:8]}"
    first_tokens = await _register_client(
        client,
        username=first_username,
        password=DEFAULT_LOGIN_VALUE,
        name="Vendor A",
    )
    second_tokens = await _register_client(
        client,
        username=second_username,
        password=DEFAULT_LOGIN_VALUE,
        name="Vendor B",
    )

    first_aic = await _submit_and_approve(
        client,
        owner_token=first_tokens["access_token"],
        staff_token=staff_tokens["access_token"],
        name=f"Agent A {uuid.uuid4().hex[:6]}",
    )
    second_aic = await _submit_and_approve(
        client,
        owner_token=second_tokens["access_token"],
        staff_token=staff_tokens["access_token"],
        name=f"Agent B {uuid.uuid4().hex[:6]}",
    )
    first_parts = first_aic.split(".")
    second_parts = second_aic.split(".")
    assert first_parts[4] == settings.aic_protocol_version
    assert first_parts[5] == settings.aic_arsp_code
    assert second_parts[5] == settings.aic_arsp_code
    assert len(first_parts[7]) == settings.aic_ontology_serial_len
    assert len(first_parts[8]) == settings.aic_instance_serial_len
    assert first_parts[6] != second_parts[6]

    same_account_aic = await _submit_and_approve(
        client,
        owner_token=first_tokens["access_token"],
        staff_token=staff_tokens["access_token"],
        name=f"Agent A2 {uuid.uuid4().hex[:6]}",
    )
    assert same_account_aic.split(".")[6] == first_parts[6]
    assert same_account_aic.split(".")[7] != first_parts[7]


async def test_preset_provider_code_is_used_on_first_approval(client, db_session) -> None:
    staff_username = f"preset-staff-{uuid.uuid4().hex[:8]}"
    staff = await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    await db_session.commit()
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    owner_username = f"preset-owner-{uuid.uuid4().hex[:8]}"
    owner_tokens = await _register_client(
        client,
        username=owner_username,
        password=DEFAULT_LOGIN_VALUE,
        name="Preset Owner",
    )
    me_response = await client.get("/api/v1/account/me", headers=_auth_headers(owner_tokens["access_token"]))
    owner_id = me_response.json()["id"]

    preset_response = await client.put(
        f"/api/v1/account/user/{owner_id}/aic-provider-code",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"aic_provider_code": "0001"},
    )
    assert preset_response.status_code == 200
    assert preset_response.json()["aic_provider_code"] == "0001"

    aic = await _submit_and_approve(
        client,
        owner_token=owner_tokens["access_token"],
        staff_token=staff_tokens["access_token"],
        name=f"Preset Agent {uuid.uuid4().hex[:6]}",
    )
    assert aic.split(".")[6] == "0001"

    in_use_response = await client.put(
        f"/api/v1/account/user/{owner_id}/aic-provider-code",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"aic_provider_code": "34C2"},
    )
    assert in_use_response.status_code == 409

    db_session.expire_all()
    result = await db_session.execute(select(Agent).where(cast("Any", Agent.created_by_id) == uuid.UUID(owner_id)))
    owned_agents = list(result.scalars().all())
    assert len(owned_agents) == 1
    old_aic = owned_agents[0].aic
    deleted_agent_id = owned_agents[0].id
    owned_agents[0].is_deleted = True
    await db_session.commit()

    db_session.expire_all()
    owner_after_delete = await db_session.get(User, uuid.UUID(owner_id))
    assert owner_after_delete is not None
    assert owner_after_delete.aic_provider_code == "0001"

    change_response = await client.put(
        f"/api/v1/account/user/{owner_id}/aic-provider-code",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"aic_provider_code": "34C2"},
    )
    assert change_response.status_code == 200
    assert change_response.json()["aic_provider_code"] == "34C2"

    new_aic = await _submit_and_approve(
        client,
        owner_token=owner_tokens["access_token"],
        staff_token=staff_tokens["access_token"],
        name=f"Reissued Agent {uuid.uuid4().hex[:6]}",
    )
    assert new_aic.split(".")[6] == "34C2"
    db_session.expire_all()
    deleted = await db_session.get(Agent, deleted_agent_id)
    assert deleted is not None
    assert deleted.aic == old_aic
    owner = await db_session.get(User, uuid.UUID(owner_id))
    assert owner is not None
    assert owner.aic_provider_code == "34C2"


async def test_disabled_agent_with_aic_blocks_provider_code_change(client, db_session) -> None:
    staff_tokens, owner_tokens, owner_id = await _create_staff_and_owner(client, db_session, prefix="disabled")
    staff_headers = _auth_headers(staff_tokens["access_token"])

    preset_response = await client.put(
        f"/api/v1/account/user/{owner_id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "0001"},
    )
    assert preset_response.status_code == 200

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={"name": f"Disabled Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert create_response.status_code == 200
    agent_id = create_response.json()["id"]
    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert submit_response.status_code == 200
    process_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=staff_headers,
        json={"approve": True, "comments": "ok"},
    )
    assert process_response.status_code == 200
    assert process_response.json()["aic"]

    disable_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/disable",
        headers=staff_headers,
        json="freeze for provider-code change",
    )
    assert disable_response.status_code == 200
    assert disable_response.json()["is_disabled"] is True
    assert disable_response.json()["is_deleted"] is False

    in_use_response = await client.put(
        f"/api/v1/account/user/{owner_id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "34C2"},
    )
    assert in_use_response.status_code == 409
    assert in_use_response.json()["error_name"] == "AIC_PROVIDER_CODE_IN_USE"


async def test_draft_and_rejected_agents_without_aic_allow_provider_code_change(client, db_session) -> None:
    staff_tokens, owner_tokens, owner_id = await _create_staff_and_owner(client, db_session, prefix="draft")
    owner_headers = _auth_headers(owner_tokens["access_token"])
    staff_headers = _auth_headers(staff_tokens["access_token"])

    draft_response = await client.post(
        "/api/v1/agent/client",
        headers=owner_headers,
        json={"name": f"Draft Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert draft_response.status_code == 200
    assert draft_response.json()["approval_status"] == "DRAFT"
    assert draft_response.json().get("aic") is None

    reject_create = await client.post(
        "/api/v1/agent/client",
        headers=owner_headers,
        json={"name": f"Rejected Agent {uuid.uuid4().hex[:6]}", "version": "1.0.0"},
    )
    assert reject_create.status_code == 200
    reject_id = reject_create.json()["id"]
    submit_response = await client.post(
        f"/api/v1/agent/client/{reject_id}/submit",
        headers=owner_headers,
    )
    assert submit_response.status_code == 200
    reject_response = await client.post(
        f"/api/v1/agent/staff/{reject_id}/process",
        headers=staff_headers,
        json={"approve": False, "comments": "needs changes"},
    )
    assert reject_response.status_code == 200
    assert reject_response.json()["approval_status"] == "REJECTED"
    assert reject_response.json().get("aic") is None

    change_response = await client.put(
        f"/api/v1/account/user/{owner_id}/aic-provider-code",
        headers=staff_headers,
        json={"aic_provider_code": "34C2"},
    )
    assert change_response.status_code == 200
    assert change_response.json()["aic_provider_code"] == "34C2"
