"""黑盒 E2E：人工审核后发布可信 provider 快照。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import cast

import pytest

from app.account.model import RoleType
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_object, response_json_string_map

pytestmark = pytest.mark.e2e
ACS_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "acs" / "beijing_urban.json"
TEST_PEER_AIC_HEADER = "X-ATR-Test-Peer-AIC"


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _mtls_test_headers(*, access_token: str, ontology_aic: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        TEST_PEER_AIC_HEADER: ontology_aic,
    }


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


def _require_entity_registration_ready(response) -> dict[str, object]:
    if response.status_code == 401:
        payload = response_json_object(response)
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if message in {
            "Valid ontology client certificate is required",
            "Client certificate identity is invalid",
        }:
            pytest.skip("target E2E server is not running with testing header override or ontology-bound client cert")

    assert response.status_code == 201
    payload = response_json_object(response)
    result = payload.get("result")
    assert isinstance(result, dict)
    return cast("dict[str, object]", result)


def _require_manual_review_status(response) -> dict[str, object]:
    assert response.status_code == 201
    payload = response_json_object(response)
    if payload["status"] == "APPROVED":
        pytest.skip("target server is running in auto-approve mode")
    assert payload["status"] == "PENDING"
    return payload


def _build_acs_payload(*, name: str) -> dict[str, object]:
    payload = cast("dict[str, object]", json.loads(ACS_FIXTURE_PATH.read_text(encoding="utf-8")))
    payload["aic"] = f"draft-{uuid.uuid4().hex[:12]}"
    payload["name"] = name
    payload["description"] = f"{name} description"
    payload["provider"] = {"organization": "Fake Org", "email": "fake@example.com", "name": "Fake Person"}
    payload["endPoints"] = [
        {
            "url": f"https://agent-{uuid.uuid4().hex[:8]}.example.com/acps-v2/{{AIC}}",
            "transport": "JSONRPC",
            "security": [{"mtls": []}],
        }
    ]
    return payload


async def test_manual_reviewed_identity_writes_trusted_provider_on_agent_approval(
    client,
    db_session,
    e2e_run_id: str,
) -> None:
    staff_username = f"provider-staff-{e2e_run_id}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="E2E Provider Staff",
    )
    await db_session.commit()

    owner_tokens = await _register_user(
        client,
        username=f"provider-owner-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="E2E Provider Owner",
    )
    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)

    identity_submit = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    identity_payload = _require_manual_review_status(identity_submit)

    identity_approve = await client.post(
        f"/api/v1/verification/admin/identity/{identity_payload['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "identity ok"},
    )
    assert identity_approve.status_code == 200

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "name": f"E2E Provider Agent {e2e_run_id}",
            "version": "1.0.0",
            "description": "created in e2e",
            "acs": _build_acs_payload(name=f"E2E Provider Agent {e2e_run_id}"),
        },
    )
    assert create_response.status_code == 200
    create_payload = response_json_object(create_response)
    agent_id = create_payload["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert submit_response.status_code == 200
    submit_payload = response_json_object(submit_response)
    assert submit_payload["approval_status"] == "PENDING"

    approve_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"approve": True, "comments": "approved in e2e"},
    )
    assert approve_response.status_code == 200
    approved_payload = response_json_object(approve_response)
    approved_acs = cast("dict[str, object]", approved_payload["acs"])
    assert approved_payload["approval_status"] == "APPROVED"
    assert approved_acs["provider"] == {
        "countryCode": "CN",
        "name": "Alice Zhang",
    }

    public_response = await client.get(f"/api/v1/agent/public/{agent_id}")
    assert public_response.status_code == 200
    public_payload = response_json_object(public_response)
    public_acs = cast("dict[str, object]", public_payload["acs"])
    assert public_acs["provider"] == {
        "countryCode": "CN",
        "name": "Alice Zhang",
    }


async def test_manual_reviewed_org_provider_is_inherited_by_atr_entity(
    client,
    mtls_client,
    db_session,
    e2e_run_id: str,
) -> None:
    staff_username = f"provider-org-staff-{e2e_run_id}"
    await create_user(
        db_session,
        username=staff_username,
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="E2E Provider Org Staff",
    )
    await db_session.commit()

    owner_tokens = await _register_user(
        client,
        username=f"provider-org-owner-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="E2E Provider Org Owner",
    )
    staff_tokens = await _login(client, username=staff_username, password=DEFAULT_LOGIN_VALUE)

    identity_submit = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    identity_payload = _require_manual_review_status(identity_submit)

    identity_approve = await client.post(
        f"/api/v1/verification/admin/identity/{identity_payload['id']}/approve",
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
    org_payload = _require_manual_review_status(org_submit)

    org_approve = await client.post(
        f"/api/v1/verification/admin/org/{org_payload['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "org ok"},
    )
    assert org_approve.status_code == 200

    create_response = await client.post(
        "/api/v1/agent/client",
        headers=_auth_headers(owner_tokens["access_token"]),
        json={
            "name": f"E2E Provider Ontology {e2e_run_id}",
            "version": "1.0.0",
            "description": "created in e2e",
            "is_ontology": True,
            "acs": _build_acs_payload(name=f"E2E Provider Ontology {e2e_run_id}"),
        },
    )
    assert create_response.status_code == 200
    create_payload = response_json_object(create_response)
    agent_id = create_payload["id"]

    submit_response = await client.post(
        f"/api/v1/agent/client/{agent_id}/submit",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert submit_response.status_code == 200
    submit_payload = response_json_object(submit_response)
    assert submit_payload["approval_status"] == "PENDING"

    approve_response = await client.post(
        f"/api/v1/agent/staff/{agent_id}/process",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"approve": True, "comments": "approved in e2e"},
    )
    assert approve_response.status_code == 200
    approved_payload = response_json_object(approve_response)
    ontology_aic = approved_payload["aic"]
    assert isinstance(ontology_aic, str)
    approved_acs = cast("dict[str, object]", approved_payload["acs"])

    expected_provider = {
        "countryCode": "CN",
        "organization": "ACPs Org",
        "license": "91310000123456789X",
    }
    assert approved_acs["provider"] == expected_provider

    entity_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        headers=_mtls_test_headers(access_token=owner_tokens["access_token"], ontology_aic=ontology_aic),
        json={
            "ontologyAic": ontology_aic,
            "endPoints": [
                {
                    "url": f"https://entity-{uuid.uuid4().hex[:8]}.example.com/callback",
                    "transport": "JSONRPC",
                    "security": [],
                }
            ],
            "entityMeta": {"scenario": "provider-inheritance"},
        },
    )
    entity_result = _require_entity_registration_ready(entity_response)
    entity_aic = entity_result["entityAic"]
    assert isinstance(entity_aic, str)

    entity_acs_response = await client.get(f"/acps-atr-v2/acs/{entity_aic}")
    assert entity_acs_response.status_code == 200
    entity_acs_payload = response_json_object(entity_acs_response)
    assert entity_acs_payload["provider"] == expected_provider
