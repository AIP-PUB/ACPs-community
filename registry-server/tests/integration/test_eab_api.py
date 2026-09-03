"""真实数据库集成测试：EAB 凭证生命周期。"""

import uuid

import pytest
from sqlalchemy import select

from app.agent.model import ApprovalStatus
from app.core.config import settings
from app.eab.model import EabCredential
from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.integration


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _internal_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.registry_server_internal_api_token}"}


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def test_generate_and_consume_eab_credential_once(client, db_session) -> None:
    owner = await create_user(
        db_session,
        username=f"owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="EAB Owner",
    )
    agent_aic = generate_aic()
    await create_agent_with_change_log(
        db_session,
        aic=agent_aic,
        name="EAB Agent",
        created_by=owner,
        approval_status=ApprovalStatus.APPROVED,
    )
    await db_session.commit()

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    create_response = await client.post(
        f"/acps-atr-v2/eab/{agent_aic}",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["aic"] == agent_aic
    assert created["keyId"]
    assert created["macKey"]

    consume_response = await client.post(
        "/internal/eab/consume",
        headers=_internal_auth_headers(),
        json={"keyId": created["keyId"]},
    )
    assert consume_response.status_code == 200
    consumed = consume_response.json()
    assert consumed == {"macKey": created["macKey"], "aic": agent_aic}

    db_session.expire_all()
    result = await db_session.execute(select(EabCredential).where(EabCredential.key_id == created["keyId"]).limit(1))
    credential = result.scalar_one()
    assert credential.is_consumed is True
    assert credential.consumed_at is not None

    second_consume_response = await client.post(
        "/internal/eab/consume",
        headers=_internal_auth_headers(),
        json={"keyId": created["keyId"]},
    )
    assert second_consume_response.status_code == 400
    assert second_consume_response.json()["error_name"] == "EAB_ALREADY_CONSUMED"


async def test_consume_eab_requires_internal_service_token(client, db_session) -> None:
    owner = await create_user(
        db_session,
        username=f"owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="EAB Owner",
    )
    agent_aic = generate_aic()
    await create_agent_with_change_log(
        db_session,
        aic=agent_aic,
        name="Protected EAB Agent",
        created_by=owner,
        approval_status=ApprovalStatus.APPROVED,
    )
    await db_session.commit()

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    create_response = await client.post(
        f"/acps-atr-v2/eab/{agent_aic}",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert create_response.status_code == 201
    created = create_response.json()

    no_token_response = await client.post(
        "/internal/eab/consume",
        json={"keyId": created["keyId"]},
    )
    assert no_token_response.status_code == 401
    assert no_token_response.json()["error_name"] == "TOKEN_VALIDATION_ERROR"

    wrong_token_response = await client.post(
        "/internal/eab/consume",
        headers={"Authorization": "Bearer wrong-token"},
        json={"keyId": created["keyId"]},
    )
    assert wrong_token_response.status_code == 401
    assert wrong_token_response.json()["error_name"] == "TOKEN_VALIDATION_ERROR"


async def test_generate_eab_rejects_non_owned_or_inactive_agent(client, db_session) -> None:
    owner = await create_user(
        db_session,
        username=f"owner-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Owner",
    )
    intruder = await create_user(
        db_session,
        username=f"intruder-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
        name="Intruder",
    )
    owned_aic = generate_aic()
    disabled_aic = generate_aic()
    await create_agent_with_change_log(
        db_session,
        aic=owned_aic,
        name="Owned Agent",
        created_by=owner,
        approval_status=ApprovalStatus.APPROVED,
    )
    await create_agent_with_change_log(
        db_session,
        aic=disabled_aic,
        name="Disabled Agent",
        created_by=owner,
        approval_status=ApprovalStatus.APPROVED,
        is_active=False,
        is_disabled=True,
    )
    await db_session.commit()

    intruder_tokens = await _login(client, username=intruder.username or "", password=DEFAULT_LOGIN_VALUE)
    not_owned_response = await client.post(
        f"/acps-atr-v2/eab/{owned_aic}",
        headers=_auth_headers(intruder_tokens["access_token"]),
    )
    assert not_owned_response.status_code == 403
    assert not_owned_response.json()["error_name"] == "AIC_NOT_OWNED"

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    inactive_response = await client.post(
        f"/acps-atr-v2/eab/{disabled_aic}",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert inactive_response.status_code == 403
    assert inactive_response.json()["error_name"] == "AIC_INACTIVE"
