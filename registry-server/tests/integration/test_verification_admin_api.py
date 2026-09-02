"""真实数据库集成测试：认证管理端详情与人工审核接口。"""

import uuid

import pytest

from app.account.model import RoleType, User
from app.core.config import settings as core_settings
from app.verification.model import IdentityVerification, OrgVerification, VerificationStatus
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.integration


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


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def test_admin_can_review_identity_detail_and_approve(client, db_session, manual_review_settings) -> None:
    staff = await create_user(
        db_session,
        username=f"staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Verification Staff",
    )
    client_user = await create_user(
        db_session,
        username=f"verify-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Verification Client",
    )
    client_user_id = client_user.id
    await db_session.commit()

    client_tokens = await _login(client, username=client_user.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    create_response = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert create_response.status_code == 201
    created_payload = create_response.json()
    assert created_payload["status"] == "PENDING"

    detail_response = await client.get(
        f"/api/v1/verification/admin/users/{client_user_id}/identity",
        headers=_auth_headers(staff_tokens["access_token"]),
    )
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["id"] == created_payload["id"]
    assert detail_payload["real_name"] == "Alice Zhang"
    assert "id_number_hash" not in detail_payload

    forbidden_response = await client.get(
        f"/api/v1/verification/admin/users/{client_user_id}/identity",
        headers=_auth_headers(client_tokens["access_token"]),
    )
    assert forbidden_response.status_code == 403

    approve_response = await client.post(
        f"/api/v1/verification/admin/identity/{created_payload['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "materials checked"},
    )
    assert approve_response.status_code == 200
    approve_payload = approve_response.json()
    assert approve_payload["status"] == "APPROVED"
    assert approve_payload["method"] == "MANUAL"
    assert approve_payload["reviewer_id"] == str(staff.id)
    assert approve_payload["remark"] == "materials checked"

    db_session.expire_all()
    record = await db_session.get(IdentityVerification, uuid.UUID(created_payload["id"]))
    refreshed_user = await db_session.get(User, client_user_id)
    assert record is not None
    assert record.status == VerificationStatus.APPROVED
    assert refreshed_user is not None
    assert refreshed_user.identity_verified is True
    assert refreshed_user.current_identity_id == record.id


async def test_admin_identity_detail_returns_null_for_existing_user_without_record(
    client,
    db_session,
    manual_review_settings,
) -> None:
    staff = await create_user(
        db_session,
        username=f"staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    client_user = await create_user(
        db_session,
        username=f"empty-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
    )
    await db_session.commit()

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    response = await client.get(
        f"/api/v1/verification/admin/users/{client_user.id}/identity",
        headers=_auth_headers(staff_tokens["access_token"]),
    )

    assert response.status_code == 200
    assert response.json() is None


async def test_admin_can_review_org_detail_and_approve(client, db_session, manual_review_settings) -> None:
    staff = await create_user(
        db_session,
        username=f"org-staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Org Staff",
    )
    client_user = await create_user(
        db_session,
        username=f"org-client-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Org Client",
    )
    client_user_id = client_user.id
    await db_session.commit()

    client_tokens = await _login(client, username=client_user.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    identity_create = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert identity_create.status_code == 201

    identity_approve = await client.post(
        f"/api/v1/verification/admin/identity/{identity_create.json()['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "identity ok"},
    )
    assert identity_approve.status_code == 200

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
    assert org_create.status_code == 201
    assert org_create.json()["status"] == "PENDING"

    detail_response = await client.get(
        f"/api/v1/verification/admin/users/{client_user_id}/org",
        headers=_auth_headers(staff_tokens["access_token"]),
    )
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["legal_rep_name"] == "Bob Li"
    assert detail_payload["org_name"] == "ACPs Org"
    assert "legal_rep_id_hash" not in detail_payload

    approve_response = await client.post(
        f"/api/v1/verification/admin/org/{org_create.json()['id']}/approve",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "org ok"},
    )
    assert approve_response.status_code == 200
    approve_payload = approve_response.json()
    assert approve_payload["status"] == "APPROVED"
    assert approve_payload["method"] == "MANUAL"
    assert approve_payload["reviewer_id"] == str(staff.id)

    db_session.expire_all()
    record = await db_session.get(OrgVerification, uuid.UUID(org_create.json()["id"]))
    refreshed_user = await db_session.get(User, client_user_id)
    assert record is not None
    assert record.status == VerificationStatus.APPROVED
    assert refreshed_user is not None
    assert refreshed_user.org_verified is True
    assert refreshed_user.current_org_id == record.id


async def test_admin_reject_requires_non_blank_remark(client, db_session, manual_review_settings) -> None:
    staff = await create_user(
        db_session,
        username=f"reject-staff-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    client_user = await create_user(
        db_session,
        username=f"reject-client-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
    )
    await db_session.commit()

    client_tokens = await _login(client, username=client_user.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)

    create_response = await client.post(
        "/api/v1/verification/identity",
        headers=_auth_headers(client_tokens["access_token"]),
        json={
            "id_type": "CN_ID_CARD",
            "id_number": "310101199001011234",
            "real_name": "Alice Zhang",
        },
    )
    assert create_response.status_code == 201

    reject_response = await client.post(
        f"/api/v1/verification/admin/identity/{create_response.json()['id']}/reject",
        headers=_auth_headers(staff_tokens["access_token"]),
        json={"remark": "   "},
    )

    assert reject_response.status_code == 422
    assert reject_response.json()["error_name"] == "VALIDATION_FAILED"
