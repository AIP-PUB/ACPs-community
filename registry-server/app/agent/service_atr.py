from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Protocol, cast

import structlog
from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.agent import service_provider
from app.agent.exception import AtrError, AtrErrorCode
from app.agent.model import Agent, ApprovalStatus
from app.sync.exception import SyncError
from app.sync.service import create_change_log, create_change_log_async
from app.utils import aic
from app.utils.utils import get_beijing_time, sha256

logger = structlog.get_logger(__name__)

type JsonObject = dict[str, object]
type JsonObjectList = list[JsonObject]
type RegistrationResult = dict[str, object]
type AgentWhereClause = ColumnElement[bool]


class OntologyAgentLike(Protocol):
    acs: object | None
    name: str
    version: str
    description: str | None
    logo_url: str | None
    created_by_id: uuid.UUID
    is_active: bool
    is_disabled: bool
    is_deleted: bool
    is_ontology: bool
    approval_status: ApprovalStatus


type OntologyAgentRecord = Agent | OntologyAgentLike

ENTITY_REGISTRATION_DB_ERROR_MESSAGE = "Database error during entity registration"
AUTO_APPROVED_ENTITY_REGISTRATION_MESSAGE = "Auto-approved via ATR entity registration"

ENTITY_ENDPOINT_CONFLICT_QUERY = text(
    """
        SELECT aic
        FROM agent
        WHERE is_active = true
            AND is_deleted = false
            AND aic != :ontology_aic
            AND acs IS NOT NULL
            AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(acs->'endPoints') AS ep
                    WHERE ep->>'url' = :url
            )
        LIMIT 1
"""
)

AGENT_AIC_COL = cast("QueryableAttribute[str | None]", Agent.aic)


def _as_agent_where_clause(value: ColumnElement[bool] | bool) -> AgentWhereClause:
    return cast("AgentWhereClause", value)


def _get_agent_acs_object(ontology_agent: OntologyAgentRecord) -> JsonObject | None:
    if not isinstance(ontology_agent.acs, dict):
        return None
    return cast("JsonObject", ontology_agent.acs)


def _coerce_string(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _build_ontology_not_found_error(ontology_aic: str) -> AtrError:
    return AtrError(
        code=AtrErrorCode.ONTOLOGY_NOT_FOUND,
        message="Ontology AIC does not exist",
        http_status=status.HTTP_404_NOT_FOUND,
        data={"ontologyAic": ontology_aic},
    )


def _build_ontology_inactive_error(ontology_aic: str, ontology_agent: OntologyAgentRecord) -> AtrError:
    return AtrError(
        code=AtrErrorCode.ONTOLOGY_INACTIVE,
        message="Ontology is inactive, disabled or deleted",
        http_status=status.HTTP_403_FORBIDDEN,
        data={
            "ontologyAic": ontology_aic,
            "isActive": ontology_agent.is_active,
            "isDisabled": ontology_agent.is_disabled,
            "isDeleted": ontology_agent.is_deleted,
        },
    )


def _build_not_ontology_error(ontology_aic: str, ontology_agent: OntologyAgentRecord) -> AtrError:
    return AtrError(
        code=AtrErrorCode.INVALID_REQUEST,
        message="The specified AIC is not an ontology. Only ontologies can derive entities.",
        http_status=status.HTTP_400_BAD_REQUEST,
        data={"ontologyAic": ontology_aic, "isOntology": ontology_agent.is_ontology},
    )


def _build_ontology_acs_missing_error(ontology_aic: str) -> AtrError:
    return AtrError(
        code=AtrErrorCode.AGENT_ACS_MISSING,
        message="Ontology ACS not found",
        http_status=status.HTTP_404_NOT_FOUND,
        data={"ontologyAic": ontology_aic},
    )


def _build_invalid_ontology_provider_error(ontology_aic: str, detail: str) -> AtrError:
    return AtrError(
        code=AtrErrorCode.INVALID_REQUEST,
        message="Ontology provider is invalid",
        http_status=status.HTTP_400_BAD_REQUEST,
        data={"ontologyAic": ontology_aic, "detail": detail},
    )


def _build_ontology_not_approved_error(ontology_aic: str, ontology_agent: OntologyAgentRecord) -> AtrError:
    return AtrError(
        code=AtrErrorCode.ONTOLOGY_INACTIVE,
        message="Ontology is not approved",
        http_status=status.HTTP_403_FORBIDDEN,
        data={
            "ontologyAic": ontology_aic,
            "approvalStatus": ontology_agent.approval_status.value,
        },
    )


def _ensure_registrable_ontology_agent(
    ontology_agent: OntologyAgentRecord | None,
    ontology_aic: str,
) -> OntologyAgentRecord:
    if not ontology_agent:
        raise _build_ontology_not_found_error(ontology_aic)

    if not ontology_agent.is_active or ontology_agent.is_disabled or ontology_agent.is_deleted:
        raise _build_ontology_inactive_error(ontology_aic, ontology_agent)

    if not ontology_agent.is_ontology:
        raise _build_not_ontology_error(ontology_aic, ontology_agent)

    if ontology_agent.approval_status != ApprovalStatus.APPROVED:
        raise _build_ontology_not_approved_error(ontology_aic, ontology_agent)

    return ontology_agent


def _get_endpoint_urls(end_points: JsonObjectList | None) -> list[str]:
    if not end_points:
        return []

    endpoint_urls: list[str] = []
    for endpoint in end_points:
        endpoint_url = endpoint.get("url")
        if isinstance(endpoint_url, str) and endpoint_url:
            endpoint_urls.append(endpoint_url)
    return endpoint_urls


def _build_endpoint_conflict_error(url: str, existing_aic: str) -> AtrError:
    return AtrError(
        code=AtrErrorCode.ENDPOINT_CONFLICT,
        message="Service endpoint URL conflicts with existing entity",
        http_status=status.HTTP_409_CONFLICT,
        data={"conflictingUrl": url, "existingAic": existing_aic},
    )


async def _ensure_entity_endpoints_available_async(
    session: AsyncSession,
    ontology_aic: str,
    end_points: JsonObjectList | None,
) -> None:
    for url in _get_endpoint_urls(end_points):
        conflict_query = await session.execute(
            ENTITY_ENDPOINT_CONFLICT_QUERY,
            {"ontology_aic": ontology_aic, "url": url},
        )
        conflict_result = conflict_query.fetchone()
        if conflict_result:
            raise _build_endpoint_conflict_error(url, conflict_result[0])


def _ensure_entity_endpoints_available(
    db: Session,
    ontology_aic: str,
    end_points: JsonObjectList | None,
) -> None:
    for url in _get_endpoint_urls(end_points):
        conflict_query = db.execute(
            ENTITY_ENDPOINT_CONFLICT_QUERY,
            {"ontology_aic": ontology_aic, "url": url},
        )
        conflict_result = conflict_query.fetchone()
        if conflict_result:
            raise _build_endpoint_conflict_error(url, conflict_result[0])


def _generate_entity_aic_or_raise(ontology_aic: str) -> str:
    entity_aic = aic.generate_entity_aic_from_ontology(ontology_aic)
    if not entity_aic:
        raise AtrError(
            code=AtrErrorCode.GENERATE_AIC_FAILED,
            message="Failed to generate entity AIC",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            data={"ontologyAic": ontology_aic},
        )
    return entity_aic


def _build_unique_entity_aic_failed_error(ontology_aic: str) -> AtrError:
    return AtrError(
        code=AtrErrorCode.GENERATE_AIC_FAILED,
        message="Failed to generate unique entity AIC after multiple attempts",
        http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        data={"ontologyAic": ontology_aic},
    )


async def _generate_unique_entity_aic_async(session: AsyncSession, ontology_aic: str) -> str:
    entity_aic = _generate_entity_aic_or_raise(ontology_aic)
    max_attempts = 10
    for attempt in range(max_attempts):
        existing_stmt = select(Agent).where(_as_agent_where_clause(entity_aic == AGENT_AIC_COL)).limit(1)
        existing_result = await session.execute(existing_stmt)
        existing = existing_result.scalar_one_or_none()
        if not existing:
            return entity_aic
        entity_aic = _generate_entity_aic_or_raise(ontology_aic)
        if attempt == max_attempts - 1:
            raise _build_unique_entity_aic_failed_error(ontology_aic)

    raise _build_unique_entity_aic_failed_error(ontology_aic)


def _generate_unique_entity_aic(db: Session, ontology_aic: str) -> str:
    entity_aic = _generate_entity_aic_or_raise(ontology_aic)
    max_attempts = 10
    for attempt in range(max_attempts):
        existing = db.query(Agent).filter(_as_agent_where_clause(entity_aic == AGENT_AIC_COL)).first()
        if not existing:
            return entity_aic
        entity_aic = _generate_entity_aic_or_raise(ontology_aic)
        if attempt == max_attempts - 1:
            raise _build_unique_entity_aic_failed_error(ontology_aic)

    raise _build_unique_entity_aic_failed_error(ontology_aic)


def _build_derived_entity_name(base_name: str, entity_aic: str) -> str:
    instance_serial = aic.get_instance_serial(entity_aic) if entity_aic else None
    suffix = instance_serial[-8:] if instance_serial else None
    if suffix:
        return f"{base_name[:246]}-{suffix}"
    return base_name[:255]


def _build_entity_acs_data(
    ontology_aic: str,
    ontology_agent: OntologyAgentRecord,
    *,
    entity_aic: str,
    current_time: datetime,
    end_points: JsonObjectList | None,
    entity_meta: JsonObject | None,
    entity_user_id: str | None,
    certificate: JsonObject | None = None,
) -> JsonObject:
    ontology_acs = _get_agent_acs_object(ontology_agent)
    if ontology_acs is None:
        raise _build_ontology_acs_missing_error(ontology_aic)

    try:
        inherited_provider = service_provider.normalize_inherited_provider(ontology_acs.get("provider"))
    except Exception as exc:
        detail = str(exc) if isinstance(exc, Exception) else "invalid provider"
        raise _build_invalid_ontology_provider_error(ontology_aic, detail) from None

    entity_acs_data: JsonObject = {
        "aic": entity_aic,
        "active": True,
        "name": ontology_acs.get("name", ontology_agent.name),
        "version": ontology_acs.get("version", ontology_agent.version),
        "provider": inherited_provider,
        "securitySchemes": ontology_acs.get("securitySchemes", {}),
        "capabilities": ontology_acs.get("capabilities", {}),
        "skills": ontology_acs.get("skills", []),
        "lastModifiedTime": current_time.isoformat(),
    }

    base_name = _coerce_string(entity_acs_data.get("name"), ontology_agent.name or "Entity")
    entity_acs_data["name"] = _build_derived_entity_name(base_name, entity_aic)

    if end_points:
        entity_acs_data["endPoints"] = end_points
    elif ontology_acs and "endPoints" in ontology_acs:
        entity_acs_data["endPoints"] = ontology_acs["endPoints"]

    if entity_meta:
        entity_acs_data["entityMeta"] = entity_meta

    if entity_user_id:
        entity_acs_data["entityUserId"] = entity_user_id

    if certificate:
        entity_acs_data["certificate"] = certificate

    return entity_acs_data


def _build_registered_entity_agent(
    ontology_agent: OntologyAgentRecord,
    *,
    entity_aic: str,
    entity_acs_data: JsonObject,
    current_time: datetime,
) -> Agent:
    entity_acs_str = json.dumps(entity_acs_data, ensure_ascii=False)
    agent_name = _coerce_string(entity_acs_data.get("name"), f"Entity of {ontology_agent.name}")
    agent_version = _coerce_string(entity_acs_data.get("version"), ontology_agent.version)

    return Agent(
        aic=entity_aic,
        name=agent_name,
        version=agent_version,
        description=ontology_agent.description,
        logo_url=ontology_agent.logo_url,
        acs=entity_acs_data,
        acs_hash=sha256(entity_acs_str),
        acs_version=1,
        is_active=True,
        is_deleted=False,
        is_disabled=False,
        created_by_id=ontology_agent.created_by_id,
        created_at=current_time,
        updated_at=current_time,
        approval_status=ApprovalStatus.APPROVED,
        submitted_at=current_time,
        processed_at=current_time,
        processed_by_id=ontology_agent.created_by_id,
        process_comments=AUTO_APPROVED_ENTITY_REGISTRATION_MESSAGE,
    )


def _build_entity_registration_result(
    ontology_aic: str,
    entity_aic: str,
    *,
    end_points: JsonObjectList | None,
    entity_meta: JsonObject | None,
    entity_user_id: str | None,
    certificate: JsonObject | None = None,
) -> RegistrationResult:
    result: RegistrationResult = {"ontologyAic": ontology_aic, "entityAic": entity_aic}
    if end_points:
        result["endPoints"] = end_points
    if entity_meta:
        result["entityMeta"] = entity_meta
    if entity_user_id:
        result["entityUserId"] = entity_user_id
    if certificate:
        result["certificate"] = certificate
    return result


def _build_entity_registration_database_error(
    ontology_aic: str,
    *,
    error: Exception | None = None,
    include_error_detail: bool,
) -> AtrError:
    data: RegistrationResult = {"ontologyAic": ontology_aic}
    if include_error_detail and error is not None:
        data["error"] = str(error)

    return AtrError(
        code=AtrErrorCode.DATABASE_ERROR,
        message=ENTITY_REGISTRATION_DB_ERROR_MESSAGE,
        http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        data=data,
    )


def _require_entity_aic(new_agent: Agent, ontology_aic: str, *, include_error_detail: bool) -> str:
    entity_aic = new_agent.aic
    if isinstance(entity_aic, str) and entity_aic:
        return entity_aic

    raise _build_entity_registration_database_error(
        ontology_aic,
        error=RuntimeError("Entity AIC is missing before changelog creation"),
        include_error_detail=include_error_detail,
    )


async def _persist_registered_entity_async(
    session: AsyncSession,
    *,
    ontology_aic: str,
    new_agent: Agent,
    result: RegistrationResult,
) -> RegistrationResult:
    try:
        session.add(new_agent)
        entity_aic = _require_entity_aic(new_agent, ontology_aic, include_error_detail=False)
        change_log = await create_change_log_async(
            session=session,
            data_type="acs",
            object_id=entity_aic,
            version=new_agent.acs_version,
            payload=new_agent.acs,
        )
        new_agent.acs_last_seq = change_log.seq
        await session.flush()
        return result
    except (SQLAlchemyError, SyncError) as error:
        logger.error(ENTITY_REGISTRATION_DB_ERROR_MESSAGE, ontology_aic=ontology_aic, error=str(error))
        raise _build_entity_registration_database_error(
            ontology_aic,
            error=error,
            include_error_detail=False,
        ) from error


def _persist_registered_entity(
    db: Session,
    *,
    ontology_aic: str,
    new_agent: Agent,
    result: RegistrationResult,
) -> RegistrationResult:
    try:
        db.add(new_agent)
        entity_aic = _require_entity_aic(new_agent, ontology_aic, include_error_detail=True)
        change_log = create_change_log(
            db=db,
            data_type="acs",
            object_id=entity_aic,
            version=new_agent.acs_version,
            payload=new_agent.acs,
        )
        new_agent.acs_last_seq = change_log.seq
        db.flush()
        return result
    except (SQLAlchemyError, SyncError) as error:
        logger.error(ENTITY_REGISTRATION_DB_ERROR_MESSAGE, ontology_aic=ontology_aic, error=str(error))
        raise _build_entity_registration_database_error(
            ontology_aic,
            error=error,
            include_error_detail=True,
        ) from None


async def register_entity_async(
    session: AsyncSession,
    ontology_aic: str,
    end_points: JsonObjectList | None = None,
    entity_meta: JsonObject | None = None,
    entity_user_id: str | None = None,
    certificate: JsonObject | None = None,
) -> RegistrationResult:
    """在 ATR 异步请求路径中注册实体。"""
    ontology_stmt = select(Agent).where(_as_agent_where_clause(ontology_aic == AGENT_AIC_COL)).limit(1)
    ontology_result = await session.execute(ontology_stmt)
    raw_ontology_agent = ontology_result.scalar_one_or_none()
    ontology_agent = _ensure_registrable_ontology_agent(raw_ontology_agent, ontology_aic)

    await _ensure_entity_endpoints_available_async(session, ontology_aic, end_points)

    entity_aic = await _generate_unique_entity_aic_async(session, ontology_aic)
    current_time = get_beijing_time()
    entity_acs_data = _build_entity_acs_data(
        ontology_aic,
        ontology_agent,
        entity_aic=entity_aic,
        current_time=current_time,
        end_points=end_points,
        entity_meta=entity_meta,
        entity_user_id=entity_user_id,
        certificate=certificate,
    )
    new_agent = _build_registered_entity_agent(
        ontology_agent,
        entity_aic=entity_aic,
        entity_acs_data=entity_acs_data,
        current_time=current_time,
    )
    result = _build_entity_registration_result(
        ontology_aic,
        entity_aic,
        end_points=end_points,
        entity_meta=entity_meta,
        entity_user_id=entity_user_id,
        certificate=certificate,
    )
    return await _persist_registered_entity_async(
        session,
        ontology_aic=ontology_aic,
        new_agent=new_agent,
        result=result,
    )


def register_entity(
    db: Session,
    ontology_aic: str,
    end_points: JsonObjectList | None = None,
    entity_meta: JsonObject | None = None,
    entity_user_id: str | None = None,
    certificate: JsonObject | None = None,
) -> RegistrationResult:
    """基于 ontology AIC 在同步路径中注册新的实体 Agent。"""
    raw_ontology_agent = db.query(Agent).filter(_as_agent_where_clause(ontology_aic == AGENT_AIC_COL)).first()
    ontology_agent = _ensure_registrable_ontology_agent(raw_ontology_agent, ontology_aic)

    _ensure_entity_endpoints_available(db, ontology_aic, end_points)

    entity_aic = _generate_unique_entity_aic(db, ontology_aic)
    current_time = get_beijing_time()
    entity_acs_data = _build_entity_acs_data(
        ontology_aic,
        ontology_agent,
        entity_aic=entity_aic,
        current_time=current_time,
        end_points=end_points,
        entity_meta=entity_meta,
        entity_user_id=entity_user_id,
        certificate=certificate,
    )
    new_agent = _build_registered_entity_agent(
        ontology_agent,
        entity_aic=entity_aic,
        entity_acs_data=entity_acs_data,
        current_time=current_time,
    )
    result = _build_entity_registration_result(
        ontology_aic,
        entity_aic,
        end_points=end_points,
        entity_meta=entity_meta,
        entity_user_id=entity_user_id,
        certificate=certificate,
    )
    return _persist_registered_entity(
        db,
        ontology_aic=ontology_aic,
        new_agent=new_agent,
        result=result,
    )
