"""黑盒 E2E：EAB 生成与消费完整流。"""

from __future__ import annotations

import pytest

from app.core.config import settings
from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.e2e


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


async def test_eab_generation_consumption_and_reuse_protection(client, db_session, e2e_run_id: str) -> None:
    owner = await create_user(
        db_session,
        username=f"eab-owner-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="EAB Owner",
    )
    intruder = await create_user(
        db_session,
        username=f"eab-intruder-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="EAB Intruder",
    )
    agent_aic = generate_aic()
    intruder_target_aic = generate_aic()

    await create_agent_with_change_log(
        db_session,
        aic=agent_aic,
        name=f"EAB Agent {e2e_run_id}",
        created_by=owner,
    )
    await create_agent_with_change_log(
        db_session,
        aic=intruder_target_aic,
        name=f"EAB Protected Agent {e2e_run_id}",
        created_by=owner,
    )
    await db_session.commit()

    owner_tokens = await _login(client, username=owner.username or "", password=DEFAULT_LOGIN_VALUE)
    intruder_tokens = await _login(client, username=intruder.username or "", password=DEFAULT_LOGIN_VALUE)

    forbidden_create = await client.post(
        f"/acps-atr-v2/eab/{intruder_target_aic}",
        headers=_auth_headers(intruder_tokens["access_token"]),
    )
    assert forbidden_create.status_code == 403
    assert forbidden_create.json()["error_name"] == "AIC_NOT_OWNED"

    create_response = await client.post(
        f"/acps-atr-v2/eab/{agent_aic}",
        headers=_auth_headers(owner_tokens["access_token"]),
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["aic"] == agent_aic
    assert created["keyId"]
    assert created["macKey"]

    no_token_consume = await client.post("/internal/eab/consume", json={"keyId": created["keyId"]})
    assert no_token_consume.status_code == 401

    consume_response = await client.post(
        "/internal/eab/consume",
        headers=_internal_auth_headers(),
        json={"keyId": created["keyId"]},
    )
    assert consume_response.status_code == 200
    assert consume_response.json() == {"macKey": created["macKey"], "aic": agent_aic}

    second_consume = await client.post(
        "/internal/eab/consume",
        headers=_internal_auth_headers(),
        json={"keyId": created["keyId"]},
    )
    assert second_consume.status_code == 400
    assert second_consume.json()["error_name"] == "EAB_ALREADY_CONSUMED"
