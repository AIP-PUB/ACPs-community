"""黑盒 E2E：ATR 实体注册主流程。"""

from typing import Any, cast

import pytest
from sqlalchemy import select

from app.agent.model import Agent, ApprovalStatus
from tests.support.aic import generate_ontology_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user

pytestmark = pytest.mark.e2e
TEST_PEER_AIC_HEADER = "X-ATR-Test-Peer-AIC"


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _mtls_test_headers(*, access_token: str, ontology_aic: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        TEST_PEER_AIC_HEADER: ontology_aic,
    }


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    assert isinstance(access_token, str)
    assert isinstance(token_type, str)
    return {
        "access_token": access_token,
        "token_type": token_type,
    }


def _require_entity_registration_ready(response) -> dict[str, object]:
    if response.status_code == 401:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if message in {
            "Valid ontology client certificate is required",
            "Client certificate identity is invalid",
        }:
            pytest.skip("target E2E server is not running with testing header override or ontology-bound client cert")

    assert response.status_code == 201
    payload = response.json()
    assert isinstance(payload, dict)
    result = payload.get("result")
    assert isinstance(result, dict)
    return result


async def test_entity_registration_creates_fetchable_entity(client, mtls_client, db_session, e2e_run_id: str) -> None:
    creator = await create_user(db_session, username=f"atr-owner-{e2e_run_id}", password=DEFAULT_LOGIN_VALUE)
    ontology_aic = generate_ontology_aic()
    await create_agent_with_change_log(
        db_session,
        aic=ontology_aic,
        name=f"Ontology-{e2e_run_id}",
        created_by=creator,
        is_ontology=True,
        approval_status=ApprovalStatus.APPROVED,
        end_points=[{"url": "https://ontology.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    tokens = await _login(client, username=creator.username or "", password=DEFAULT_LOGIN_VALUE)

    create_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        headers=_mtls_test_headers(access_token=tokens["access_token"], ontology_aic=ontology_aic),
        json={
            "ontologyAic": ontology_aic,
            "endPoints": [
                {
                    "url": f"https://entity-{e2e_run_id}.example.com/callback",
                    "transport": "JSONRPC",
                    "security": [],
                }
            ],
            "entityMeta": {"scenario": "e2e"},
        },
    )

    created = _require_entity_registration_ready(create_response)
    entity_aic = created["entityAic"]
    assert isinstance(entity_aic, str)
    assert created["ontologyAic"] == ontology_aic
    assert entity_aic.split(".")[:8] == ontology_aic.split(".")[:8]
    assert created["entityMeta"] == {"scenario": "e2e"}

    db_session.expire_all()
    result = await db_session.execute(select(Agent).where(cast("Any", Agent.aic) == entity_aic).limit(1))
    entity = result.scalar_one_or_none()
    assert entity is not None
    assert entity.acs is not None
    assert entity.acs["entityMeta"] == {"scenario": "e2e"}

    acs_response = await client.get(f"/acps-atr-v2/acs/{entity_aic}")
    assert acs_response.status_code == 200
    assert acs_response.json()["aic"] == entity_aic
    assert acs_response.json()["active"] is True
