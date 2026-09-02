from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.account.model import User
from app.agent import service_provider
from app.agent.exception import AgentErrorCode
from app.verification.model import (
    IdentityDocumentType,
    IdentityVerification,
    OrgVerification,
    VerificationStatus,
)

pytestmark = pytest.mark.unit


def _as_async_session(session: object) -> AsyncSession:
    return cast("AsyncSession", session)


def _as_session(session: object) -> Session:
    return cast("Session", session)


def _build_user(*, identity_verified: bool = False, org_verified: bool = False) -> User:
    user = User(id=uuid.uuid4(), username=f"user-{uuid.uuid4().hex[:8]}", is_active=True)
    user.identity_verified = identity_verified
    user.org_verified = org_verified
    user.current_identity_id = uuid.uuid4() if identity_verified else None
    user.current_org_id = uuid.uuid4() if org_verified else None
    return user


def _build_identity_record(
    user_id: uuid.UUID,
    *,
    id_type: IdentityDocumentType = IdentityDocumentType.CN_ID_CARD,
) -> IdentityVerification:
    return IdentityVerification(
        id=uuid.uuid4(),
        user_id=user_id,
        id_type=id_type,
        id_number_hash="salt$hash",
        real_name_encrypted="cipher",
        status=VerificationStatus.APPROVED,
    )


def _build_org_record(user_id: uuid.UUID) -> OrgVerification:
    return OrgVerification(
        id=uuid.uuid4(),
        user_id=user_id,
        org_name="ACPs Org",
        usci="91310000123456789X",
        legal_rep_name_encrypted="cipher",
        legal_rep_id_hash="salt$hash",
        status=VerificationStatus.APPROVED,
    )


def test_validate_agent_provider_schema_rejects_unknown_field() -> None:
    with pytest.raises(Exception) as exc_info:
        service_provider.validate_agent_provider_schema({"unexpected": "value"})

    assert getattr(exc_info.value, "error_name", None) == AgentErrorCode.INVALID_ACS


def test_normalize_inherited_provider_none_returns_empty_object() -> None:
    assert service_provider.normalize_inherited_provider(None) == {}


def test_normalize_inherited_provider_rejects_non_object() -> None:
    with pytest.raises(Exception) as exc_info:
        service_provider.normalize_inherited_provider("not-an-object")

    assert getattr(exc_info.value, "error_name", None) == AgentErrorCode.INVALID_ACS


@pytest.mark.asyncio
async def test_build_verified_provider_snapshot_async_prefers_organization(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _build_user(identity_verified=True, org_verified=True)
    identity_record = _build_identity_record(user.id)
    user.current_identity_id = identity_record.id
    org_record = _build_org_record(user.id)
    user.current_org_id = org_record.id

    monkeypatch.setattr(service_provider, "_get_user_for_provider_async", AsyncMock(return_value=user))
    monkeypatch.setattr(service_provider, "_get_current_org_record_async", AsyncMock(return_value=org_record))
    identity_mock = AsyncMock(return_value=identity_record)
    monkeypatch.setattr(service_provider, "_get_current_identity_record_async", identity_mock)

    provider = await service_provider.build_verified_provider_snapshot_async(_as_async_session(object()), user.id)

    assert provider == {
        "countryCode": "CN",
        "organization": "ACPs Org",
        "license": "91310000123456789X",
    }
    assert identity_mock.await_count == 1


@pytest.mark.asyncio
async def test_build_verified_provider_snapshot_async_falls_back_to_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _build_user(identity_verified=True, org_verified=False)
    identity_record = _build_identity_record(user.id)
    user.current_identity_id = identity_record.id

    monkeypatch.setattr(service_provider, "_get_user_for_provider_async", AsyncMock(return_value=user))
    monkeypatch.setattr(service_provider, "_get_current_org_record_async", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service_provider,
        "_get_current_identity_record_async",
        AsyncMock(return_value=identity_record),
    )
    monkeypatch.setattr(service_provider, "sm4_decrypt", lambda ciphertext, key: "Alice Zhang")

    provider = await service_provider.build_verified_provider_snapshot_async(_as_async_session(object()), user.id)

    assert provider == {"countryCode": "CN", "name": "Alice Zhang"}


@pytest.mark.asyncio
async def test_build_verified_provider_snapshot_async_returns_empty_for_unverified_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _build_user(identity_verified=False, org_verified=False)

    monkeypatch.setattr(service_provider, "_get_user_for_provider_async", AsyncMock(return_value=user))
    monkeypatch.setattr(service_provider, "_get_current_org_record_async", AsyncMock(return_value=None))
    monkeypatch.setattr(service_provider, "_get_current_identity_record_async", AsyncMock(return_value=None))

    provider = await service_provider.build_verified_provider_snapshot_async(_as_async_session(object()), user.id)

    assert provider == {}


@pytest.mark.asyncio
async def test_build_verified_provider_snapshot_async_rejects_inconsistent_identity_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _build_user(identity_verified=True, org_verified=False)
    user.current_identity_id = None

    monkeypatch.setattr(service_provider, "_get_user_for_provider_async", AsyncMock(return_value=user))
    monkeypatch.setattr(service_provider, "_get_current_org_record_async", AsyncMock(return_value=None))

    with pytest.raises(Exception) as exc_info:
        await service_provider.build_verified_provider_snapshot_async(_as_async_session(object()), user.id)

    assert getattr(exc_info.value, "error_name", None) == AgentErrorCode.INVALID_ACS


@pytest.mark.asyncio
async def test_build_verified_provider_snapshot_async_rejects_org_without_valid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _build_user(identity_verified=False, org_verified=True)
    org_record = _build_org_record(user.id)
    user.current_org_id = org_record.id

    monkeypatch.setattr(service_provider, "_get_user_for_provider_async", AsyncMock(return_value=user))
    monkeypatch.setattr(service_provider, "_get_current_org_record_async", AsyncMock(return_value=org_record))
    monkeypatch.setattr(service_provider, "_get_current_identity_record_async", AsyncMock(return_value=None))

    with pytest.raises(Exception) as exc_info:
        await service_provider.build_verified_provider_snapshot_async(_as_async_session(object()), user.id)

    assert getattr(exc_info.value, "error_name", None) == AgentErrorCode.INVALID_ACS


@pytest.mark.asyncio
async def test_apply_verified_provider_snapshot_async_updates_json_string_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_id=uuid.uuid4(),
        acs='{"name":"Demo","version":"1.0.0","provider":{"name":"Draft"}}',
    )

    monkeypatch.setattr(
        service_provider,
        "build_verified_provider_snapshot_async",
        AsyncMock(return_value={"name": "Trusted Provider"}),
    )

    changed = await service_provider.apply_verified_provider_snapshot_async(
        _as_async_session(object()),
        cast("Any", agent),
    )

    assert changed is True
    assert agent.acs["provider"] == {"name": "Trusted Provider"}
    assert "lastModifiedTime" in agent.acs


def test_apply_verified_provider_snapshot_sync_skips_missing_acs() -> None:
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_id=uuid.uuid4(),
        acs=None,
    )

    changed = service_provider.apply_verified_provider_snapshot(_as_session(object()), cast("Any", agent))

    assert changed is False


def test_build_verified_provider_snapshot_sync_rejects_org_without_valid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _build_user(identity_verified=False, org_verified=True)
    org_record = _build_org_record(user.id)
    user.current_org_id = org_record.id

    monkeypatch.setattr(service_provider, "_get_user_for_provider", lambda db, user_id: user)
    monkeypatch.setattr(service_provider, "_get_current_org_record", lambda db, current_user: org_record)
    monkeypatch.setattr(service_provider, "_get_current_identity_record", lambda db, current_user: None)

    with pytest.raises(Exception) as exc_info:
        service_provider.build_verified_provider_snapshot(_as_session(object()), user.id)

    assert getattr(exc_info.value, "error_name", None) == AgentErrorCode.INVALID_ACS
