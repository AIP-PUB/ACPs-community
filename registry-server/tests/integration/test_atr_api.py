"""真实数据库集成测试：ATR 主路径。"""

import pytest
from sqlalchemy import select

from app.account.model import RoleType
from app.agent.exception import AtrErrorCode
from app.agent.model import Agent, ApprovalStatus
from tests.support.aic import generate_aic, generate_ontology_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user

pytestmark = pytest.mark.integration
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


async def test_get_agent_not_found_uses_real_database(client) -> None:
    response = await client.get(f"/acps-atr-v2/acs/{generate_aic()}")

    assert response.status_code == 404
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["error"]["message"] == "Agent not found with the provided AIC"


async def test_register_entity_creates_entity_and_exposes_acs(client, mtls_client, db_session) -> None:
    creator = await create_user(db_session, username="ontology-owner", password=DEFAULT_LOGIN_VALUE)
    ontology_aic = generate_ontology_aic()
    await create_agent_with_change_log(
        db_session,
        aic=ontology_aic,
        name="Ontology Agent",
        created_by=creator,
        is_ontology=True,
        approval_status=ApprovalStatus.APPROVED,
        end_points=[{"url": "https://ontology.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    payload = {
        "ontologyAic": ontology_aic,
        "endPoints": [
            {
                "url": "https://entity.example.com/callback",
                "transport": "JSONRPC",
                "security": [],
            }
        ],
        "entityMeta": {"tenant": "integration"},
        "certificate": {
            "altNames": {
                "dns": ["entity.example.com"],
                "ip": ["10.0.1.50"],
            },
            "requestedValidity": 365,
        },
    }

    public_response = await client.post("/acps-atr-v2/entity", json=payload)
    assert public_response.status_code == 404

    unauthenticated_response = await mtls_client.post("/acps-atr-v2/entity", json=payload)
    assert unauthenticated_response.status_code == 401

    tokens = await _login(client, username=creator.username or "", password=DEFAULT_LOGIN_VALUE)

    missing_certificate_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json=payload,
        headers=_auth_headers(tokens["access_token"]),
    )
    assert missing_certificate_response.status_code == 401

    non_ontology_certificate_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json=payload,
        headers=_mtls_test_headers(access_token=tokens["access_token"], ontology_aic=generate_aic()),
    )
    assert non_ontology_certificate_response.status_code == 403

    mismatched_certificate_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json=payload,
        headers=_mtls_test_headers(access_token=tokens["access_token"], ontology_aic=generate_ontology_aic()),
    )
    assert mismatched_certificate_response.status_code == 403

    headers = _mtls_test_headers(access_token=tokens["access_token"], ontology_aic=ontology_aic)

    response = await mtls_client.post("/acps-atr-v2/entity", json=payload, headers=headers)

    assert response.status_code == 201
    entity_aic = response.json()["result"]["entityAic"]
    assert entity_aic != ontology_aic
    assert response.json()["result"]["entityMeta"] == {"tenant": "integration"}
    assert response.json()["result"]["certificate"] == {
        "altNames": {
            "dns": ["entity.example.com"],
            "ip": ["10.0.1.50"],
        },
        "requestedValidity": 365,
    }

    db_session.expire_all()
    result = await db_session.execute(select(Agent).where(Agent.aic == entity_aic).limit(1))
    entity = result.scalar_one_or_none()
    assert entity is not None
    assert entity.is_active is True
    assert entity.approval_status == ApprovalStatus.APPROVED
    assert entity.acs is not None
    assert entity.acs["entityMeta"] == {"tenant": "integration"}
    assert entity.acs["endPoints"][0]["url"] == "https://entity.example.com/callback"
    assert entity.acs["certificate"] == {
        "altNames": {
            "dns": ["entity.example.com"],
            "ip": ["10.0.1.50"],
        },
        "requestedValidity": 365,
    }

    acs_response = await client.get(f"/acps-atr-v2/acs/{entity_aic}")
    assert acs_response.status_code == 200
    assert acs_response.json()["aic"] == entity_aic
    assert acs_response.json()["active"] is True
    assert acs_response.json()["certificate"]["altNames"]["dns"] == ["entity.example.com"]


async def test_register_entity_rejects_non_owner_without_staff_role(client, mtls_client, db_session) -> None:
    creator = await create_user(db_session, username="ontology-owner-2", password=DEFAULT_LOGIN_VALUE)
    intruder = await create_user(db_session, username="ontology-intruder", password=DEFAULT_LOGIN_VALUE)
    staff = await create_user(
        db_session,
        username="ontology-staff",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
    )
    ontology_aic = generate_ontology_aic()
    await create_agent_with_change_log(
        db_session,
        aic=ontology_aic,
        name="Ontology Agent 2",
        created_by=creator,
        is_ontology=True,
        approval_status=ApprovalStatus.APPROVED,
        end_points=[{"url": "https://ontology-2.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    payload = {
        "ontologyAic": ontology_aic,
        "endPoints": [
            {
                "url": "https://entity-forbidden.example.com/callback",
                "transport": "JSONRPC",
                "security": [],
            }
        ],
    }

    intruder_tokens = await _login(client, username=intruder.username or "", password=DEFAULT_LOGIN_VALUE)
    intruder_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json=payload,
        headers=_mtls_test_headers(access_token=intruder_tokens["access_token"], ontology_aic=ontology_aic),
    )
    assert intruder_response.status_code == 403
    assert intruder_response.json()["error"]["code"] == 40301

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)
    staff_response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json=payload,
        headers=_mtls_test_headers(access_token=staff_tokens["access_token"], ontology_aic=ontology_aic),
    )
    assert staff_response.status_code == 201


async def test_mtls_plane_does_not_expose_public_auth_routes(mtls_client) -> None:
    response = await mtls_client.post(
        "/api/v1/auth/login",
        data={"username": "nobody", "password": DEFAULT_LOGIN_VALUE},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("case_name", "is_active", "is_disabled", "is_deleted", "approval_status"),
    [
        ("inactive", False, False, False, ApprovalStatus.APPROVED),
        ("disabled", True, True, False, ApprovalStatus.APPROVED),
        ("deleted", True, False, True, ApprovalStatus.APPROVED),
        ("not-approved", True, False, False, ApprovalStatus.PENDING),
    ],
)
async def test_register_entity_rejects_unregistrable_ontology_states(
    client,
    mtls_client,
    db_session,
    case_name: str,
    is_active: bool,
    is_disabled: bool,
    is_deleted: bool,
    approval_status: ApprovalStatus,
) -> None:
    creator = await create_user(
        db_session,
        username=f"ontology-owner-{case_name}",
        password=DEFAULT_LOGIN_VALUE,
    )
    ontology_aic = generate_ontology_aic()
    await create_agent_with_change_log(
        db_session,
        aic=ontology_aic,
        name=f"Ontology Agent {case_name}",
        created_by=creator,
        is_ontology=True,
        approval_status=approval_status,
        is_active=is_active,
        is_disabled=is_disabled,
        is_deleted=is_deleted,
        end_points=[{"url": f"https://ontology-{case_name}.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    tokens = await _login(client, username=creator.username or "", password=DEFAULT_LOGIN_VALUE)

    response = await mtls_client.post(
        "/acps-atr-v2/entity",
        json={
            "ontologyAic": ontology_aic,
            "endPoints": [
                {
                    "url": f"https://entity-{case_name}.example.com/callback",
                    "transport": "JSONRPC",
                    "security": [],
                }
            ],
        },
        headers=_mtls_test_headers(access_token=tokens["access_token"], ontology_aic=ontology_aic),
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["error"]["code"] == int(AtrErrorCode.ONTOLOGY_INACTIVE)
    assert payload["error"]["message"] == "Ontology is inactive, disabled, deleted or not approved"
    assert payload["error"]["data"] == {
        "ontologyAic": ontology_aic,
        "isActive": is_active,
        "isDisabled": is_disabled,
        "isDeleted": is_deleted,
        "approvalStatus": approval_status.value,
    }


async def test_mtls_entity_endpoint_rejects_disallowed_ip(blocked_mtls_client, db_session) -> None:
    creator = await create_user(db_session, username="ontology-owner-3", password=DEFAULT_LOGIN_VALUE)
    ontology_aic = generate_ontology_aic()
    await create_agent_with_change_log(
        db_session,
        aic=ontology_aic,
        name="Ontology Agent 3",
        created_by=creator,
        is_ontology=True,
        approval_status=ApprovalStatus.APPROVED,
        end_points=[{"url": "https://ontology-3.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    response = await blocked_mtls_client.post(
        "/acps-atr-v2/entity",
        json={"ontologyAic": ontology_aic},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Access denied: IP address not allowed"
