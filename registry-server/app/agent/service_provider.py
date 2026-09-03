from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import status
from jsonschema import ValidationError
from jsonschema import validate as json_validate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.account.model import User
from app.agent import service_acs
from app.agent.exception import AgentError, AgentErrorCode, SchemaFileMissingError
from app.core.config import settings
from app.core.crypto import sm4_decrypt
from app.utils.utils import get_beijing_time
from app.verification.model import (
    IdentityDocumentType,
    IdentityVerification,
    OrgVerification,
    VerificationStatus,
)

if TYPE_CHECKING:
    from app.agent.model import Agent

type JsonObject = dict[str, object]
type ProviderWhereClause = ColumnElement[bool]


USER_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", User.id)
IDENTITY_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", IdentityVerification.id)
IDENTITY_DELETED_AT_COLUMN = cast("QueryableAttribute[datetime | None]", IdentityVerification.deleted_at)
ORG_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", OrgVerification.id)
ORG_DELETED_AT_COLUMN = cast("QueryableAttribute[datetime | None]", OrgVerification.deleted_at)


def _as_provider_clause(value: ColumnElement[bool] | bool) -> ProviderWhereClause:
    return cast("ProviderWhereClause", value)


def _build_provider_error(
    message: str,
    *,
    input_params: dict[str, object] | None = None,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> AgentError:
    return AgentError(
        status_code=status_code,
        error_name=AgentErrorCode.INVALID_ACS,
        error_msg=message,
        input_params=input_params or {},
    )


@lru_cache(maxsize=1)
def _load_agent_schema() -> JsonObject:
    schema_path = Path(__file__).with_name("acsSchema.json")
    if not schema_path.exists():
        raise SchemaFileMissingError()

    with schema_path.open(encoding="utf-8") as file:
        schema = json.load(file)

    if not isinstance(schema, dict):
        raise _build_provider_error(
            "ACS schema must be a JSON object",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return cast("JsonObject", schema)


def _load_agent_provider_schema() -> JsonObject:
    root_schema = _load_agent_schema()
    defs = root_schema.get("$defs")
    if not isinstance(defs, dict) or "AgentProvider" not in defs:
        raise _build_provider_error(
            "AgentProvider schema definition is missing",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return {
        "$schema": root_schema.get("$schema"),
        "$ref": "#/$defs/AgentProvider",
        "$defs": defs,
    }


def load_agent_provider_required_fields() -> list[str]:
    defs = _load_agent_schema().get("$defs")
    if not isinstance(defs, dict):
        return []

    provider_schema = defs.get("AgentProvider")
    if not isinstance(provider_schema, dict):
        return []

    raw_required = provider_schema.get("required")
    if not isinstance(raw_required, list):
        return []

    return [str(field) for field in raw_required if isinstance(field, str)]


def validate_agent_provider_schema(provider: JsonObject) -> None:
    try:
        json_validate(instance=provider, schema=_load_agent_provider_schema())
    except ValidationError as exc:
        raise _build_provider_error(
            f"Provider schema invalid. Json path: [ {exc.json_path} ]; Error message: [ {exc.message} ]",
            input_params={"provider": provider},
        ) from None


def ensure_provider_required_fields(provider: JsonObject) -> None:
    missing_fields = [
        field
        for field in load_agent_provider_required_fields()
        if field not in provider or provider[field] in (None, "", [], {})
    ]
    if missing_fields:
        raise _build_provider_error(
            f"Trusted provider snapshot is missing required fields: {', '.join(missing_fields)}",
            input_params={"provider": provider, "missing_fields": missing_fields},
            status_code=status.HTTP_409_CONFLICT,
        )


def _strip_empty_values(provider: JsonObject) -> JsonObject:
    cleaned: JsonObject = {}
    for key, value in provider.items():
        if value is None:
            continue
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                continue
            cleaned[key] = normalized
            continue
        if isinstance(value, list):
            if not value:
                continue
            cleaned[key] = value
            continue
        if isinstance(value, dict):
            nested = _strip_empty_values(cast("JsonObject", value))
            if not nested:
                continue
            cleaned[key] = nested
            continue
        cleaned[key] = value
    return cleaned


def _validate_provider_snapshot(provider: JsonObject) -> JsonObject:
    cleaned = _strip_empty_values(provider)
    validate_agent_provider_schema(cleaned)
    ensure_provider_required_fields(cleaned)
    return cleaned


def normalize_inherited_provider(provider: object) -> JsonObject:
    if provider is None:
        return _validate_provider_snapshot({})

    if not isinstance(provider, dict):
        raise _build_provider_error(
            "Inherited provider must be a JSON object",
            input_params={"provider": str(provider)},
        )

    return _validate_provider_snapshot(cast("JsonObject", copy.deepcopy(provider)))


def _decrypt_real_name(record: IdentityVerification) -> str:
    try:
        return sm4_decrypt(record.real_name_encrypted, settings.sm4_encryption_key)
    except Exception as exc:
        raise _build_provider_error(
            "Failed to decrypt verified identity real_name",
            input_params={"verification_id": str(record.id), "field_name": "real_name"},
            status_code=status.HTTP_409_CONFLICT,
        ) from exc


def _build_identity_provider(record: IdentityVerification) -> JsonObject:
    provider: JsonObject = {
        "name": _decrypt_real_name(record),
    }
    if record.id_type == IdentityDocumentType.CN_ID_CARD:
        provider["countryCode"] = "CN"
    return provider


def _build_org_provider(record: OrgVerification) -> JsonObject:
    provider: JsonObject = {
        "countryCode": "CN",
        "organization": record.org_name,
    }
    if record.usci:
        provider["license"] = record.usci
    elif record.org_registration_number:
        provider["license"] = record.org_registration_number
    return provider


def _ensure_org_snapshot_has_identity(
    *,
    user: User,
    identity_record: IdentityVerification | None,
    org_record: OrgVerification,
) -> None:
    if identity_record is not None:
        return

    raise _build_provider_error(
        "Verified organization state is inconsistent: approved identity verification is required",
        input_params={
            "user_id": str(user.id),
            "current_org_id": str(org_record.id),
            "current_identity_id": str(user.current_identity_id) if user.current_identity_id else None,
        },
        status_code=status.HTTP_409_CONFLICT,
    )


async def _get_user_for_provider_async(session: AsyncSession, user_id: uuid.UUID) -> User:
    stmt = select(User).where(_as_provider_clause(user_id == USER_ID_COLUMN)).limit(1).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise _build_provider_error(
            "Agent owner not found while building verified provider snapshot",
            input_params={"user_id": str(user_id)},
            status_code=status.HTTP_409_CONFLICT,
        )
    return user


def _get_user_for_provider(db: Session, user_id: uuid.UUID) -> User:
    stmt = select(User).where(_as_provider_clause(user_id == USER_ID_COLUMN)).limit(1).with_for_update()
    result = db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise _build_provider_error(
            "Agent owner not found while building verified provider snapshot",
            input_params={"user_id": str(user_id)},
            status_code=status.HTTP_409_CONFLICT,
        )
    return user


async def _get_current_identity_record_async(
    session: AsyncSession,
    user: User,
) -> IdentityVerification | None:
    if not user.identity_verified:
        return None
    if user.current_identity_id is None:
        raise _build_provider_error(
            "Verified identity state is inconsistent: current_identity_id is missing",
            input_params={"user_id": str(user.id)},
            status_code=status.HTTP_409_CONFLICT,
        )

    stmt = (
        select(IdentityVerification)
        .where(
            _as_provider_clause(user.current_identity_id == IDENTITY_ID_COLUMN),
            IDENTITY_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None or record.user_id != user.id or record.status != VerificationStatus.APPROVED:
        raise _build_provider_error(
            "Verified identity state is inconsistent with current_identity_id",
            input_params={
                "user_id": str(user.id),
                "current_identity_id": str(user.current_identity_id),
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return record


def _get_current_identity_record(
    db: Session,
    user: User,
) -> IdentityVerification | None:
    if not user.identity_verified:
        return None
    if user.current_identity_id is None:
        raise _build_provider_error(
            "Verified identity state is inconsistent: current_identity_id is missing",
            input_params={"user_id": str(user.id)},
            status_code=status.HTTP_409_CONFLICT,
        )

    stmt = (
        select(IdentityVerification)
        .where(
            _as_provider_clause(user.current_identity_id == IDENTITY_ID_COLUMN),
            IDENTITY_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = db.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None or record.user_id != user.id or record.status != VerificationStatus.APPROVED:
        raise _build_provider_error(
            "Verified identity state is inconsistent with current_identity_id",
            input_params={
                "user_id": str(user.id),
                "current_identity_id": str(user.current_identity_id),
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return record


async def _get_current_org_record_async(
    session: AsyncSession,
    user: User,
) -> OrgVerification | None:
    if not user.org_verified:
        return None
    if user.current_org_id is None:
        raise _build_provider_error(
            "Verified organization state is inconsistent: current_org_id is missing",
            input_params={"user_id": str(user.id)},
            status_code=status.HTTP_409_CONFLICT,
        )

    stmt = (
        select(OrgVerification)
        .where(
            _as_provider_clause(user.current_org_id == ORG_ID_COLUMN),
            ORG_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None or record.user_id != user.id or record.status != VerificationStatus.APPROVED:
        raise _build_provider_error(
            "Verified organization state is inconsistent with current_org_id",
            input_params={
                "user_id": str(user.id),
                "current_org_id": str(user.current_org_id),
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return record


def _get_current_org_record(
    db: Session,
    user: User,
) -> OrgVerification | None:
    if not user.org_verified:
        return None
    if user.current_org_id is None:
        raise _build_provider_error(
            "Verified organization state is inconsistent: current_org_id is missing",
            input_params={"user_id": str(user.id)},
            status_code=status.HTTP_409_CONFLICT,
        )

    stmt = (
        select(OrgVerification)
        .where(
            _as_provider_clause(user.current_org_id == ORG_ID_COLUMN),
            ORG_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = db.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None or record.user_id != user.id or record.status != VerificationStatus.APPROVED:
        raise _build_provider_error(
            "Verified organization state is inconsistent with current_org_id",
            input_params={
                "user_id": str(user.id),
                "current_org_id": str(user.current_org_id),
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return record


async def build_verified_provider_snapshot_async(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> JsonObject:
    user = await _get_user_for_provider_async(session, user_id)
    org_record = await _get_current_org_record_async(session, user)
    identity_record: IdentityVerification | None = None
    if org_record is not None or user.identity_verified:
        identity_record = await _get_current_identity_record_async(session, user)
    if org_record is not None:
        _ensure_org_snapshot_has_identity(user=user, identity_record=identity_record, org_record=org_record)
        return _validate_provider_snapshot(_build_org_provider(org_record))

    if identity_record is not None:
        return _validate_provider_snapshot(_build_identity_provider(identity_record))

    return _validate_provider_snapshot({})


def build_verified_provider_snapshot(
    db: Session,
    user_id: uuid.UUID,
) -> JsonObject:
    user = _get_user_for_provider(db, user_id)
    org_record = _get_current_org_record(db, user)
    identity_record: IdentityVerification | None = None
    if org_record is not None or user.identity_verified:
        identity_record = _get_current_identity_record(db, user)
    if org_record is not None:
        _ensure_org_snapshot_has_identity(user=user, identity_record=identity_record, org_record=org_record)
        return _validate_provider_snapshot(_build_org_provider(org_record))

    if identity_record is not None:
        return _validate_provider_snapshot(_build_identity_provider(identity_record))

    return _validate_provider_snapshot({})


async def apply_verified_provider_snapshot_async(session: AsyncSession, agent: Agent) -> bool:
    if not agent.acs:
        return False

    acs_data = service_acs._load_agent_acs_data(agent)
    trusted_provider = await build_verified_provider_snapshot_async(session, agent.created_by_id)
    current_provider = acs_data.get("provider")
    if current_provider == trusted_provider:
        return False

    acs_data["provider"] = trusted_provider
    acs_data["lastModifiedTime"] = get_beijing_time().isoformat()
    agent.acs = acs_data
    return True


def apply_verified_provider_snapshot(db: Session, agent: Agent) -> bool:
    if not agent.acs:
        return False

    acs_data = service_acs._load_agent_acs_data(agent)
    trusted_provider = build_verified_provider_snapshot(db, agent.created_by_id)
    current_provider = acs_data.get("provider")
    if current_provider == trusted_provider:
        return False

    acs_data["provider"] = trusted_provider
    acs_data["lastModifiedTime"] = get_beijing_time().isoformat()
    agent.acs = acs_data
    return True
