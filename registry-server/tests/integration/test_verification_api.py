"""真实数据库集成测试：verification 主流程与状态持久化。"""

import uuid

import pytest

from app.account.model import User
from app.verification.model import IdentityVerification, OrgVerification, VerificationStatus
from tests.support.constants import DEFAULT_LOGIN_VALUE
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


async def test_identity_and_org_verification_workflow_persists_status(client, db_session) -> None:
    client_user = await create_user(
        db_session,
        username=f"verify-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Verification Client",
    )
    client_user_id = client_user.id
    await db_session.commit()

    token_data = await _login(client, username=client_user.username or "", password=DEFAULT_LOGIN_VALUE)
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

    db_session.expire_all()
    identity_record = await db_session.get(IdentityVerification, uuid.UUID(identity_payload["id"]))
    refreshed_user = await db_session.get(User, client_user_id)
    assert identity_record is not None
    assert identity_record.status == VerificationStatus.APPROVED
    assert refreshed_user is not None
    assert refreshed_user.identity_verified is True
    assert refreshed_user.current_identity_id == identity_record.id

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

    db_session.expire_all()
    org_record = await db_session.get(OrgVerification, uuid.UUID(org_payload["id"]))
    refreshed_user = await db_session.get(User, client_user_id)
    assert org_record is not None
    assert org_record.status == VerificationStatus.APPROVED
    assert refreshed_user is not None
    assert refreshed_user.org_verified is True
    assert refreshed_user.current_org_id == org_record.id

    org_read = await client.get("/api/v1/verification/org", headers=headers)
    assert org_read.status_code == 200
    assert org_read.json()["id"] == org_payload["id"]
    assert org_read.json()["status"] == "APPROVED"


async def test_identity_verification_rejects_whitespace_only_required_fields(client, db_session) -> None:
    client_user = await create_user(
        db_session,
        username=f"verify-invalid-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Verification Client",
    )
    await db_session.commit()

    token_data = await _login(client, username=client_user.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(token_data["access_token"])

    response = await client.post(
        "/api/v1/verification/identity",
        headers=headers,
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "   ",
            "real_name": "  Alice Zhang  ",
        },
    )

    assert response.status_code == 422
    assert response.json()["error_name"] == "VALIDATION_FAILED"
