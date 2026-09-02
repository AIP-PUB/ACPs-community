"""黑盒 E2E：身份与组织校验主流程。"""

import pytest

from tests.support.constants import DEFAULT_LOGIN_VALUE
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


async def test_identity_then_org_verification_flow(client, e2e_run_id: str) -> None:
    token_data = await _register_user(
        client,
        username=f"verify-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="Verifier",
    )
    headers = _auth_headers(token_data["access_token"])

    identity_initial = await client.get("/api/v1/verification/identity", headers=headers)
    assert identity_initial.status_code == 200
    assert identity_initial.json() is None

    org_initial = await client.get("/api/v1/verification/org", headers=headers)
    assert org_initial.status_code == 200
    assert org_initial.json() is None

    org_before_identity = await client.post(
        "/api/v1/verification/org",
        headers=headers,
        json={"org_name": "ACPs Org", "usci": "91310000123456789X"},
    )
    assert org_before_identity.status_code == 403
    assert org_before_identity.json()["error_name"] == "IDENTITY_NOT_VERIFIED"

    identity_create = await client.post(
        "/api/v1/verification/identity",
        headers=headers,
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert identity_create.status_code == 201
    identity_payload = identity_create.json()
    assert identity_payload["status"] == "APPROVED"

    identity_repeat = await client.post(
        "/api/v1/verification/identity",
        headers=headers,
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert identity_repeat.status_code == 409
    assert identity_repeat.json()["error_name"] == "IDENTITY_ALREADY_VERIFIED"

    identity_read = await client.get("/api/v1/verification/identity", headers=headers)
    assert identity_read.status_code == 200
    assert identity_read.json()["id"] == identity_payload["id"]
    assert identity_read.json()["status"] == "APPROVED"

    org_create = await client.post(
        "/api/v1/verification/org",
        headers=headers,
        json={
            "org_name": "ACPs Org",
            "usci": "91310000123456789X",
            "legal_rep_name": "Bob Li",
            "legal_rep_id_number": "310101199201019999",
        },
    )
    assert org_create.status_code == 201
    org_payload = org_create.json()
    assert org_payload["status"] == "APPROVED"

    org_read = await client.get("/api/v1/verification/org", headers=headers)
    assert org_read.status_code == 200
    assert org_read.json()["id"] == org_payload["id"]
    assert org_read.json()["status"] == "APPROVED"
