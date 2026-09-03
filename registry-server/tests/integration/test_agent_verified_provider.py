"""真实数据库集成测试：ACS 可信 provider 快照与 ATR 继承。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select

from app.account.model import RoleType
from app.agent.model import Agent
from app.core.config import settings as core_settings
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.integration
TEST_PEER_AIC_HEADER = "X-ATR-Test-Peer-AIC"
ACS_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "acs" / "beijing_urban.json"


@pytest.fixture
def manual_review_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        type(core_settings),
        "auto_approve_identity_verification",
        property(lambda self: False),
    )
    monkeypatch.setattr(
        type(core_settings),
        "auto_approve_org_verification",
        property(lambda self: False),
    )


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _mtls_test_headers(*, access_token: str, ontology_aic: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        TEST_PEER_AIC_HEADER: ontology_aic,
    }


def _build_acs_payload(
    *,
    name: str,
    version: str = "1.0.0",
    provider: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = cast("dict[str, object]", json.loads(ACS_FIXTURE_PATH.read_text(encoding="utf-8")))
    payload["aic"] = f"draft-{uuid.uuid4().hex[:12]}"
    payload["name"] = name
    payload["version"] = version
    payload["description"] = f"{name} description"
    payload["provider"] = provider or {"organization": "Fake Org", "email": "fake@example.com"}
    payload["endPoints"] = [
        {
            "url": f"https://agent-{uuid.uuid4().hex[:8]}.example.com/acps-v2/{{AIC}}",
            "transport": "JSONRPC",
            "security": [{"mtls": []}],
        }
    ]
    return payload


def _response_json_object(response) -> dict[str, object]:
    return cast("dict[str, object]", response.json())


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def _create_agent_draft(
    client,
    *,
    access_token: str,
    name: str,
    acs: dict[str, object],
    is_ontology: bool = False,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(access_token),
        json={
            "name": name,
            "version": "1.0.0",
            "description": f"{name} draft",
            "acs": acs,
            "is_ontology": is_ontology,
        },
    )
    assert response.status_code == 200
    return _response_json_object(response)


async def _submit_agent(client, *, access_token: str, agent_id: str) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(access_token),
    )
    assert response.status_code == 200
    return _response_json_object(response)


async def _approve_agent(client, *, access_token: str, agent_id: str, comments: str = "approved") -> dict[str, object]:
    response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(access_token),
        json={"approve": True, "comments": comments},
    )
    assert response.status_code == 200
    return _response_json_object(response)


async def test_manual_review_chain_writes_trusted_org_provider_and_entity_inherits_it(
    client,
    mtls_client,
    db_session,
    manual_review_settings,
) -> None:
    staff = await create_user(
        db_session,
        username=f"staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Provider Staff",
    )
    owner = await create_user(
        db_session,
        username=f"owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Provider Owner",
    )
    await db_session.commit()

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    identity_submit = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert identity_submit.status_code == 201
    assert identity_submit.json()["status"] == "PENDING"

    identity_approve = await client.post(
        f"/api/v1/verification/admin/identity/{identity_submit.json()['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "identity ok"},
    )
    assert identity_approve.status_code == 200

    org_submit = await client.post(
        "/api/v1/verification/org",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "org_name": "ACPs Org",
            "usci": "91310000123456789X",
            "legal_rep_name": "Bob Li",
            "legal_rep_id_number": "310101199201019999",
        },
    )
    assert org_submit.status_code == 201
    assert org_submit.json()["status"] == "PENDING"

    org_approve = await client.post(
        f"/api/v1/verification/admin/org/{org_submit.json()['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "org ok"},
    )
    assert org_approve.status_code == 200

    draft = await _create_agent_draft(
        client,
        access_token=owner_tokens["access_token"],
        name=f"Ontology Agent {uuid.uuid4().hex[:6]}",
        is_ontology=True,
        acs=_build_acs_payload(
            name="Ontology Agent",
            provider={"organization": "Fake Org", "email": "fake@example.com", "name": "Fake Person"},
        ),
    )

    await _submit_agent(client, access_token=owner_tokens["access_token"], agent_id=str(draft["id"]))
    approved_agent = await _approve_agent(
        client,
        access_token=staff_tokens["access_token"],
        agent_id=str(draft["id"]),
        comments="publish ontology",
    )

    expected_provider = {
        "countryCode": "CN",
        "organization": "ACPs Org",
        "license": "91310000123456789X",
    }
    approved_acs = cast("dict[str, object]", approved_agent["acs"])
    assert approved_acs["provider"] == expected_provider
    ontology_aic = approved_agent["aic"]
    assert isinstance(ontology_aic, str)

    entity_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json={
            "ontologyAic": ontology_aic,
            "endPoints": [
                {
                    "url": f"https://entity-{uuid.uuid4().hex[:8]}.example.com/callback",
                    "transport": "JSONRPC",
                    "security": [],
                }
            ],
        },
        headers=_mtls_test_headers(access_token=owner_tokens["access_token"], ontology_aic=ontology_aic),
    )
    assert entity_response.status_code == 201
    entity_payload = _response_json_object(entity_response)
    entity_result = cast("dict[str, object]", entity_payload["result"])
    entity_aic = entity_result["entityAic"]

    db_session.expire_all()
    result = await db_session.execute(select(Agent).where(cast("Any", Agent.aic) == entity_aic).limit(1))
    entity = result.scalar_one_or_none()
    assert entity is not None
    assert entity.acs["provider"] == expected_provider


async def test_agent_approval_writes_trusted_identity_provider_when_only_identity_verified(
    client,
    db_session,
    manual_review_settings,
) -> None:
    staff = await create_user(
        db_session,
        username=f"id-staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    owner = await create_user(
        db_session,
        username=f"id-owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
    )
    await db_session.commit()

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    identity_submit = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert identity_submit.status_code == 201

    identity_approve = await client.post(
        f"/api/v1/verification/admin/identity/{identity_submit.json()['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "identity ok"},
    )
    assert identity_approve.status_code == 200

    draft = await _create_agent_draft(
        client,
        access_token=owner_tokens["access_token"],
        name=f"Identity Agent {uuid.uuid4().hex[:6]}",
        acs=_build_acs_payload(
            name="Identity Agent",
            provider={"organization": "Fake Org", "email": "fake@example.com"},
        ),
    )

    await _submit_agent(client, access_token=owner_tokens["access_token"], agent_id=str(draft["id"]))
    approved_agent = await _approve_agent(
        client,
        access_token=staff_tokens["access_token"],
        agent_id=str(draft["id"]),
        comments="publish identity agent",
    )

    approved_acs = cast("dict[str, object]", approved_agent["acs"])
    assert approved_acs["provider"] == {
        "countryCode": "CN",
        "name": "Alice Zhang",
    }


async def test_unverified_agent_approval_writes_empty_provider(client, db_session) -> None:
    staff = await create_user(
        db_session,
        username=f"empty-staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    owner = await create_user(
        db_session,
        username=f"empty-owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
    )
    await db_session.commit()

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    draft = await _create_agent_draft(
        client,
        access_token=owner_tokens["access_token"],
        name=f"Empty Provider Agent {uuid.uuid4().hex[:6]}",
        acs=_build_acs_payload(
            name="Empty Provider Agent",
            provider={"organization": "Fake Org", "email": "fake@example.com"},
        ),
    )

    await _submit_agent(client, access_token=owner_tokens["access_token"], agent_id=str(draft["id"]))
    approved_agent = await _approve_agent(
        client,
        access_token=staff_tokens["access_token"],
        agent_id=str(draft["id"]),
        comments="publish empty provider",
    )

    approved_acs = cast("dict[str, object]", approved_agent["acs"])
    assert approved_acs["provider"] == {}
