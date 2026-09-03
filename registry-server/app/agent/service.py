import json
import uuid
from datetime import datetime
from typing import Any, cast

import httpx
import nh3
import structlog
from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.account import service_account as account_service_module
from app.account.model import RoleType, User
from app.agent import service_acs, service_atr, service_command, service_provider, service_query
from app.agent.exception import (
    AgentError,
    AgentErrorCode,
)
from app.agent.model import Agent, ApprovalStatus
from app.core.config import settings
from app.sync.service import (
    create_change_log,
    create_change_log_async,
    update_agent_with_changelog_async,
)
from app.sync.service import (
    update_agent_with_changelog as sync_update_agent_with_changelog,
)
from app.utils import aic
from app.utils.utils import get_beijing_time, sha256

logger = structlog.get_logger(__name__)

INVALID_ACS_JSON_MESSAGE = "Invalid JSON format for acs field"
AGENT_INACTIVE_MESSAGE = "Cannot update an inactive agent"
STAFF_USER_NOT_FOUND_MESSAGE = "Staff user not found"

type AgentWhereClause = ColumnElement[bool]

AGENT_ID_COL = service_query.AGENT_ID_COL
AGENT_AIC_COL = service_query.AGENT_AIC_COL
AGENT_NAME_COL = service_query.AGENT_NAME_COL
AGENT_VERSION_COL = service_query.AGENT_VERSION_COL
AGENT_CREATED_BY_ID_COL = service_query.AGENT_CREATED_BY_ID_COL
AGENT_PROCESSED_BY_ID_COL = service_query.AGENT_PROCESSED_BY_ID_COL
AGENT_APPROVAL_STATUS_COL = service_query.AGENT_APPROVAL_STATUS_COL
AGENT_IS_ACTIVE_COL = service_query.AGENT_IS_ACTIVE_COL
AGENT_IS_DELETED_COL = service_query.AGENT_IS_DELETED_COL
AGENT_IS_DISABLED_COL = service_query.AGENT_IS_DISABLED_COL
AGENT_IS_ONTOLOGY_COL = service_query.AGENT_IS_ONTOLOGY_COL
AGENT_CREATED_AT_COL = service_query.AGENT_CREATED_AT_COL
AGENT_PROCESSED_AT_COL = service_query.AGENT_PROCESSED_AT_COL
AGENT_CREATED_BY_REL = service_query.AGENT_CREATED_BY_REL
AGENT_PROCESSED_BY_REL = service_query.AGENT_PROCESSED_BY_REL
USER_ROLES_REL = service_query.USER_ROLES_REL

get_agent = service_query.get_agent
get_agent_async = service_query.get_agent_async
get_agent_by_aic = service_query.get_agent_by_aic
get_agent_by_aic_async = service_query.get_agent_by_aic_async
get_agents = service_query.get_agents
get_agents_async = service_query.get_agents_async
get_recent_agents = service_query.get_recent_agents
get_recent_agents_async = service_query.get_recent_agents_async
create_agent_response = service_query.create_agent_response
create_agent_detail_response = service_query.create_agent_detail_response
get_user_async = account_service_module.get_user_async
acs_service_module = cast("Any", service_acs)
atr_service_module = cast("Any", service_atr)
provider_service_module = cast("Any", service_provider)


def _as_agent_where_clause(value: ColumnElement[bool] | bool) -> AgentWhereClause:
    return cast("AgentWhereClause", value)


async def update_agent_acs_data_async(agent: Agent, session: AsyncSession | None = None) -> None:
    """兼容旧导入面，在 service.py 中委托 ACS 异步更新。"""
    acs_service_module.get_beijing_time = get_beijing_time
    acs_service_module.update_agent_with_changelog_async = update_agent_with_changelog_async
    await service_acs.update_agent_acs_data_async(agent, session)


def update_agent_acs_data(agent: Agent, db: Session | None = None) -> None:
    """兼容旧导入面，在 service.py 中委托 ACS 同步更新。"""
    acs_service_module.get_beijing_time = get_beijing_time
    service_acs.update_agent_acs_data(agent, db)


async def generate_aic_for_agent_async(session: AsyncSession, agent: Agent) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步 AIC 生成。"""
    acs_service_module.get_beijing_time = get_beijing_time
    acs_service_module.update_agent_with_changelog_async = update_agent_with_changelog_async
    return await service_acs.generate_aic_for_agent_async(session, agent)


def generate_aic_for_agent(db: Session, agent: Agent) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步 AIC 生成。"""
    acs_service_module.get_beijing_time = get_beijing_time
    return service_acs.generate_aic_for_agent(db, agent)


async def build_verified_provider_snapshot_async(session: AsyncSession, user_id: uuid.UUID) -> dict[str, object]:
    """兼容旧导入面，在 service.py 中委托异步 provider 快照生成。"""
    return await service_provider.build_verified_provider_snapshot_async(session, user_id)


def build_verified_provider_snapshot(db: Session, user_id: uuid.UUID) -> dict[str, object]:
    """兼容旧导入面，在 service.py 中委托同步 provider 快照生成。"""
    return service_provider.build_verified_provider_snapshot(db, user_id)


async def apply_verified_provider_snapshot_async(session: AsyncSession, agent: Agent) -> bool:
    """兼容旧导入面，在 service.py 中委托异步 provider 快照写入。"""
    return await service_provider.apply_verified_provider_snapshot_async(session, agent)


def apply_verified_provider_snapshot(db: Session, agent: Agent) -> bool:
    """兼容旧导入面，在 service.py 中委托同步 provider 快照写入。"""
    return service_provider.apply_verified_provider_snapshot(db, agent)


def update_agent_with_changelog(db: Session, agent: Agent, agent_data: dict[str, Any]) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步变更日志更新。"""
    return sync_update_agent_with_changelog(db, agent, agent_data)


def normalize_inherited_provider(provider: object) -> dict[str, object]:
    """兼容旧导入面，在 service.py 中委托 provider 继承归一化。"""
    return service_provider.normalize_inherited_provider(provider)


async def create_agent_async(session: AsyncSession, user_id: uuid.UUID, agent_data: dict[str, Any]) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步创建命令。"""
    return await service_command.create_agent_async(session, user_id, agent_data)


async def update_agent_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_data: dict[str, Any],
) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步更新命令。"""
    return await service_command.update_agent_async(session, agent_id, user_id, agent_data)


async def delete_agent_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str = "User deletion",
) -> bool:
    """兼容旧导入面，在 service.py 中委托异步删除命令。"""
    return await service_command.delete_agent_async(session, agent_id, user_id, reason=reason)


async def batch_delete_agents_async(
    session: AsyncSession,
    agent_ids: list[uuid.UUID],
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """兼容旧导入面，在 service.py 中委托异步批量删除命令。"""
    return await service_command.batch_delete_agents_async(session, agent_ids, user_id)


async def disable_agent_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    staff_user_id: uuid.UUID,
    reason: str = "Staff disable",
) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步禁用命令。"""
    return await service_command.disable_agent_async(session, agent_id, staff_user_id, reason=reason)


async def enable_agent_async(session: AsyncSession, agent_id: uuid.UUID, staff_user_id: uuid.UUID) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步启用命令。"""
    return await service_command.enable_agent_async(session, agent_id, staff_user_id)


async def submit_agent_for_approval_async(session: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步提交审核命令。"""
    return await service_command.submit_agent_for_approval_async(session, agent_id, user_id)


async def cancel_agent_submission_async(session: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步撤销提交命令。"""
    return await service_command.cancel_agent_submission_async(session, agent_id, user_id)


async def process_agent_approval_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    processor_id: uuid.UUID,
    approve: bool,
    comments: str | None = None,
) -> Agent:
    """兼容旧导入面，在 service.py 中委托异步审核处理命令。"""
    return await service_command.process_agent_approval_async(
        session,
        agent_id,
        processor_id,
        approve,
        comments=comments,
    )


def delete_agent(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID, reason: str = "User deletion") -> bool:
    """兼容旧导入面，在 service.py 中委托同步删除命令。"""
    return service_command.delete_agent(db, agent_id, user_id, reason=reason)


def batch_delete_agents(db: Session, agent_ids: list[uuid.UUID], user_id: uuid.UUID) -> dict[str, Any]:
    """兼容旧导入面，在 service.py 中委托同步批量删除命令。"""
    return service_command.batch_delete_agents(db, agent_ids, user_id)


def disable_agent(
    db: Session,
    agent_id: uuid.UUID,
    staff_user_id: uuid.UUID,
    reason: str = "Staff disable",
) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步禁用命令。"""
    return service_command.disable_agent(db, agent_id, staff_user_id, reason=reason)


def enable_agent(db: Session, agent_id: uuid.UUID, staff_user_id: uuid.UUID) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步启用命令。"""
    return service_command.enable_agent(db, agent_id, staff_user_id)


def create_agent(db: Session, user_id: uuid.UUID, agent_data: dict[str, Any]) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步创建命令。"""
    return service_command.create_agent(db, user_id, agent_data)


def update_agent(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID, agent_data: dict[str, Any]) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步更新命令。"""
    return service_command.update_agent(db, agent_id, user_id, agent_data)


def submit_agent_for_approval(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步提交审核命令。"""
    return service_command.submit_agent_for_approval(db, agent_id, user_id)


def cancel_agent_submission(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步撤销提交命令。"""
    return service_command.cancel_agent_submission(db, agent_id, user_id)


def process_agent_approval(
    db: Session,
    agent_id: uuid.UUID,
    processor_id: uuid.UUID,
    approve: bool,
    comments: str | None = None,
) -> Agent:
    """兼容旧导入面，在 service.py 中委托同步审核处理命令。"""
    return service_command.process_agent_approval(db, agent_id, processor_id, approve, comments=comments)


async def register_entity_async(
    session: AsyncSession,
    ontology_aic: str,
    end_points: list[dict[str, Any]] | None = None,
    entity_meta: dict[str, Any] | None = None,
    entity_user_id: str | None = None,
    certificate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """兼容旧导入面，在 service.py 中委托 ATR 异步实体注册。"""
    atr_service_module.create_change_log_async = create_change_log_async
    return await service_atr.register_entity_async(
        session=session,
        ontology_aic=ontology_aic,
        end_points=end_points,
        entity_meta=entity_meta,
        entity_user_id=entity_user_id,
        certificate=certificate,
    )


def register_entity(
    db: Session,
    ontology_aic: str,
    end_points: list[dict[str, Any]] | None = None,
    entity_meta: dict[str, Any] | None = None,
    entity_user_id: str | None = None,
    certificate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """兼容旧导入面，在 service.py 中委托 ATR 同步实体注册。"""
    atr_service_module.create_change_log = create_change_log
    return service_atr.register_entity(
        db=db,
        ontology_aic=ontology_aic,
        end_points=end_points,
        entity_meta=entity_meta,
        entity_user_id=entity_user_id,
        certificate=certificate,
    )


def _has_staff_processing_role(user: User) -> bool:
    """返回用户是否具备操作工作人员审核端点的权限。"""

    return any(role.name in {RoleType.STAFF, RoleType.ADMIN} for role in user.roles)


def _build_name_already_claimed_error(name: str) -> AgentError:
    return AgentError(
        status_code=status.HTTP_403_FORBIDDEN,
        error_name=AgentErrorCode.AGENT_NAME_ALREADY_CLAIMED,
        error_msg=f"The name '{name}' is already owned by another user. Please choose a different name.",
        input_params={"name": name},
    )


def _build_name_version_exists_error(name: str, version: str) -> AgentError:
    return AgentError(
        status_code=status.HTTP_409_CONFLICT,
        error_name=AgentErrorCode.AGENT_NAME_VERSION_EXISTS,
        error_msg=f"Agent with name '{name}' and version '{version}' already exists",
        input_params={"name": name, "version": version},
    )


def _build_acs_error_input(acs_value: str) -> dict[str, str]:
    return {"acs": acs_value[:100] if len(acs_value) > 100 else acs_value}


def _sanitize_plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    return nh3.clean(value, tags=set())


def _sanitize_agent_write_payload(payload: dict[str, Any]) -> None:
    description = payload.get("description")
    if isinstance(description, str) or description is None:
        payload["description"] = _sanitize_plain_text(description)


def _build_staff_user_not_found_error(staff_user_id: uuid.UUID) -> AgentError:
    return AgentError(
        status_code=status.HTTP_404_NOT_FOUND,
        error_name=AgentErrorCode.PROCESSOR_NOT_FOUND,
        error_msg=STAFF_USER_NOT_FOUND_MESSAGE,
        input_params={"staff_user_id": str(staff_user_id)},
    )


def _normalize_agent_acs_payload(payload: dict[str, Any], *, error_name: AgentErrorCode) -> str | None:
    """校验 ACS 载荷，将 JSON 字符串规范化为 dict，并在需要时设置 `acs_hash`。"""
    acs_value = payload.get("acs")
    if not acs_value:
        return None

    from app.utils.acs import validate as validate_acs

    if isinstance(acs_value, str):
        validate_acs(acs_value)
        acs_hash = sha256(acs_value)
        try:
            payload["acs"] = json.loads(acs_value)
        except json.JSONDecodeError:
            raise AgentError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=error_name,
                error_msg=INVALID_ACS_JSON_MESSAGE,
                input_params=_build_acs_error_input(acs_value),
            ) from None
        payload["acs_hash"] = acs_hash
        return acs_hash

    if isinstance(acs_value, dict):
        acs_string = json.dumps(acs_value, ensure_ascii=False)
        validate_acs(acs_string)
        acs_hash = sha256(acs_string)
        payload["acs_hash"] = acs_hash
        return acs_hash

    return None


def _is_agent_deleted(agent: Agent) -> bool:
    return bool(getattr(agent, "is_deleted", False) or getattr(agent, "deleted_at", None) is not None)


def _is_agent_disabled(agent: Agent) -> bool:
    return bool(getattr(agent, "is_disabled", False) or getattr(agent, "disabled_at", None) is not None)


def _ensure_agent_is_active(agent: Agent, agent_id: uuid.UUID) -> None:
    if not agent.is_active:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.AGENT_INACTIVE,
            error_msg=AGENT_INACTIVE_MESSAGE,
            input_params={"agent_id": str(agent_id)},
        )


def _ensure_agent_can_be_deleted(agent: Agent, agent_id: uuid.UUID) -> None:
    if _is_agent_deleted(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Deleted agents cannot be deleted again",
            input_params={"agent_id": str(agent_id)},
        )


def _ensure_agent_can_be_disabled(agent: Agent, agent_id: uuid.UUID) -> None:
    if _is_agent_deleted(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Deleted agents cannot be disabled",
            input_params={"agent_id": str(agent_id)},
        )

    if _is_agent_disabled(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Disabled agents cannot be disabled again",
            input_params={"agent_id": str(agent_id)},
        )


def _ensure_agent_can_be_enabled(agent: Agent, agent_id: uuid.UUID) -> None:
    if _is_agent_deleted(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Deleted agents cannot be enabled",
            input_params={"agent_id": str(agent_id)},
        )

    if not _is_agent_disabled(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only disabled agents can be enabled",
            input_params={"agent_id": str(agent_id)},
        )


def _ensure_agent_is_transitionable_for_approval(agent: Agent, agent_id: uuid.UUID, *, action: str) -> None:
    if _is_agent_deleted(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg=f"Deleted agents cannot be {action}",
            input_params={"agent_id": str(agent_id)},
        )

    if _is_agent_disabled(agent):
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg=f"Disabled agents cannot be {action}",
            input_params={"agent_id": str(agent_id)},
        )

    _ensure_agent_is_active(agent, agent_id)


def _clear_agent_processing_metadata(agent: Agent) -> None:
    agent.processed_by_id = None
    agent.processed_at = None
    agent.process_comments = None


def _mark_agent_pending(agent: Agent) -> None:
    _clear_agent_processing_metadata(agent)
    agent.approval_status = ApprovalStatus.PENDING
    agent.submitted_at = get_beijing_time()
    agent.updated_at = get_beijing_time()


def _mark_agent_draft(agent: Agent) -> None:
    _clear_agent_processing_metadata(agent)
    agent.approval_status = ApprovalStatus.DRAFT
    agent.submitted_at = None
    agent.updated_at = get_beijing_time()


def _mark_agent_processed(agent: Agent, *, processor_id: uuid.UUID, approve: bool, comments: str | None) -> None:
    agent.approval_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    agent.processed_by_id = processor_id
    agent.processed_at = get_beijing_time()
    agent.process_comments = _sanitize_plain_text(comments)
    agent.updated_at = get_beijing_time()


def _get_derived_entity_like_prefix(agent: Agent) -> str | None:
    if not agent.is_ontology or not agent.aic:
        return None
    return aic.get_derived_entity_like_prefix(agent.aic) or "__invalid_aic_prefix__"


def _mark_agent_deleted(agent: Agent, *, current_time: datetime, reason: str) -> None:
    agent.is_active = False
    agent.is_deleted = True
    agent.deleted_at = current_time
    agent.deleted_reason = reason
    agent.updated_at = current_time


def _mark_agent_disabled(agent: Agent, *, current_time: datetime, reason: str) -> None:
    agent.is_active = False
    agent.is_disabled = True
    agent.disabled_at = current_time
    agent.disabled_reason = reason
    agent.updated_at = current_time


def _mark_agent_enabled(agent: Agent, *, current_time: datetime) -> None:
    agent.is_disabled = False
    agent.disabled_at = None
    agent.disabled_reason = None
    agent.updated_at = current_time
    if not _is_agent_deleted(agent):
        agent.is_active = True


def _ensure_agent_is_editable(agent: Agent, agent_id: uuid.UUID, user_id: uuid.UUID) -> None:
    _ensure_agent_is_active(agent, agent_id)

    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only update your own agents",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )

    if agent.approval_status in [ApprovalStatus.APPROVED, ApprovalStatus.PENDING]:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg=f"Agents in {agent.approval_status} status cannot be updated",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )


def _get_target_agent_identity(payload: dict[str, Any], agent: Agent) -> tuple[str, str]:
    return cast("str", payload.get("name", agent.name)), cast("str", payload.get("version", agent.version))


def _get_changed_agent_identity(payload: dict[str, Any], agent: Agent) -> tuple[str, str] | None:
    name, version = _get_target_agent_identity(payload, agent)
    if name == agent.name and version == agent.version:
        return None
    return name, version


async def _ensure_agent_identity_available_async(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    name: str,
    version: str,
) -> None:
    existing_agent_stmt = (
        select(Agent)
        .where(
            _as_agent_where_clause(name == AGENT_NAME_COL),
            _as_agent_where_clause(version == AGENT_VERSION_COL),
            _as_agent_where_clause(agent_id != AGENT_ID_COL),
            AGENT_IS_ACTIVE_COL.is_(True),
        )
        .limit(1)
    )
    existing_agent_result = await session.execute(existing_agent_stmt)
    existing_agent = existing_agent_result.scalar_one_or_none()
    if existing_agent:
        raise _build_name_version_exists_error(name, version)


def _ensure_agent_identity_available(db: Session, *, agent_id: uuid.UUID, name: str, version: str) -> None:
    existing_agent = (
        db.query(Agent)
        .filter(
            _as_agent_where_clause(name == AGENT_NAME_COL),
            _as_agent_where_clause(version == AGENT_VERSION_COL),
            _as_agent_where_clause(agent_id != AGENT_ID_COL),
            AGENT_IS_ACTIVE_COL.is_(True),
        )
        .first()
    )
    if existing_agent:
        raise _build_name_version_exists_error(name, version)


def _prepare_agent_update_payload(
    payload: dict[str, Any],
    *,
    current_acs_hash: str | None,
    error_name: AgentErrorCode,
) -> bool:
    if "acs" not in payload:
        return False

    new_acs_hash = _normalize_agent_acs_payload(payload, error_name=error_name)
    return bool(new_acs_hash and new_acs_hash != current_acs_hash)


def _apply_agent_payload(agent: Agent, payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        if hasattr(agent, key):
            setattr(agent, key, value)


async def _sync_agent_update_async(session: AsyncSession, agent: Agent, *, acs_updated: bool) -> None:
    if acs_updated:
        await update_agent_with_changelog_async(session, agent, {"acs": agent.acs})
        return

    await update_agent_acs_data_async(agent, session)


def _sync_agent_update(db: Session, agent: Agent, *, acs_updated: bool) -> None:
    if acs_updated:
        from app.sync.service import update_agent_with_changelog

        update_agent_with_changelog(db, agent, {"acs": agent.acs})
        return

    update_agent_acs_data(agent, db)


def notify_ca_server_revoke_cert(agent: Agent, reason: int = 5) -> None:
    """
    通知 CA Server 吊销指定 Agent 的证书（使用 ATR 协议）

    Args:
        agent: Agent对象，需要包含AIC
        reason: 吊销原因代码，默认为5 (cessationOfOperation)
               - 0: unspecified（未指定）
               - 1: keyCompromise（密钥泄露）
               - 2: cACompromise（CA 泄露）
               - 3: affiliationChanged（隶属关系变更）
               - 4: superseded（被替代）
               - 5: cessationOfOperation（停止运营）
    """
    if not agent.aic:
        # 如果没有AIC，则跳过证书revoke操作
        return

    # Mock 模式：跳过真实调用，直接记录日志并返回
    if settings.ca_server_mock:
        logger.info("Mock 模式下跳过 CA Server 吊销通知", agent_aic=agent.aic, reason=reason)
        return

    try:
        # 构造 CA Server 的管理接口 URL
        ca_server_url = getattr(settings, "ca_server_atr_base_url", None)
        if not ca_server_url:
            # CA Server URL 未配置，抛异常
            raise AgentError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=AgentErrorCode.REMOTE_CERT_REVOKE_FAILED,
                error_msg="CA Server URL is not configured",
                input_params={"agent_aic": agent.aic, "error_type": "config_error"},
            )

        revoke_url = f"{ca_server_url.rstrip('/')}/ca/revoke-notify"

        # 构造请求体
        revoke_request = {"aic": agent.aic, "reason": reason}
        internal_service_token = getattr(settings, "registry_server_internal_api_token", "").strip()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ACPs-Registry-Server/1.0",
        }
        if internal_service_token:
            headers["Authorization"] = f"Bearer {internal_service_token}"

        # 发送吊销通知给 CA Server
        response = httpx.post(
            revoke_url,
            json=revoke_request,
            headers=headers,
            timeout=30,
        )

        # 记录结果，但不抛出异常以免影响主流程
        if response.status_code == 200:
            # 证书吊销通知成功
            logger.info("已成功通知 CA Server 吊销证书", agent_aic=agent.aic)
        else:
            # CA Server 返回错误，记录日志但不阻断流程
            logger.error(
                "CA Server 吊销证书时返回非成功状态码",
                agent_aic=agent.aic,
                status_code=response.status_code,
                response_text=response.text,
            )

    except httpx.HTTPError as e:
        # 网络错误，记录日志但不阻断流程
        logger.error("通知 CA Server 吊销证书时发生网络错误", agent_aic=agent.aic, error=str(e))
    except (AgentError, AttributeError, TypeError, ValueError) as e:
        # 其他未预期的错误，记录日志但不阻断流程
        logger.error(
            "通知 CA Server 吊销证书时发生未预期错误",
            agent_aic=agent.aic,
            error=str(e),
        )


def _resolve_json_schema_reference(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any] | None:
    ref_path = schema.get("$ref")
    if not isinstance(ref_path, str) or not ref_path.startswith("#/"):
        return None

    current: Any = root_schema
    for part in ref_path.split("/")[1:]:
        if not isinstance(current, dict):
            return {}
        current = current.get(part, {})

    return current if isinstance(current, dict) else {}


def _select_first_composite_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("anyOf", "oneOf", "allOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            first_option = options[0]
            if isinstance(first_option, dict):
                return first_option
    return None


def _get_schema_example(schema: dict[str, Any]) -> tuple[Any, bool]:
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0], True
    return None, False


def _format_jsonc_example(example_val: Any, spaces: str) -> str:
    return json.dumps(example_val, ensure_ascii=False, indent=2).replace("\n", "\n" + spaces)


def _append_jsonc_property_line(
    lines: list[str],
    *,
    spaces: str,
    prop_name: str,
    val_str: str,
    child_desc: str,
    has_comma: bool,
) -> None:
    comma = "," if has_comma else ""
    line_prefix = f'{spaces}  "{prop_name}": '
    normalized_desc = child_desc.replace("\n", " ")

    if "\n" not in val_str:
        line = f"{line_prefix}{val_str}{comma}"
        if normalized_desc:
            line += f" // {normalized_desc}"
        lines.append(line)
        return

    if normalized_desc:
        lines.append(f"{spaces}  // {normalized_desc}")
    lines.append(f"{line_prefix}{val_str}{comma}")


def _render_object_jsonc_sample(
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    *,
    spaces: str,
    indent: int,
    description: str,
    example_val: Any,
    has_example: bool,
) -> tuple[str, str]:
    properties = schema.get("properties", {})
    if not properties:
        if has_example:
            return _format_jsonc_example(example_val, spaces), description
        return "{}", description

    lines = ["{"]
    prop_items = list(properties.items())
    for index, (prop_name, prop_schema) in enumerate(prop_items):
        val_str, child_desc = generate_jsonc_sample_from_schema(prop_schema, root_schema, indent + 2)
        _append_jsonc_property_line(
            lines,
            spaces=spaces,
            prop_name=prop_name,
            val_str=val_str,
            child_desc=child_desc,
            has_comma=index < len(prop_items) - 1,
        )

    lines.append(f"{spaces}}}")
    return "\n".join(lines), description


def _render_array_jsonc_sample(
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    *,
    spaces: str,
    indent: int,
    description: str,
    example_val: Any,
    has_example: bool,
) -> tuple[str, str]:
    items_schema = schema.get("items", {})
    item_type = items_schema.get("type")

    if has_example and item_type in {"string", "number", "integer", "boolean"}:
        return _format_jsonc_example(example_val, spaces), description

    val_str, _item_desc = generate_jsonc_sample_from_schema(items_schema, root_schema, indent + 2)
    lines = ["[", f"{spaces}  {val_str}", f"{spaces}]"]
    return "\n".join(lines), description


def _render_scalar_jsonc_sample(
    type_: str | None, example_val: Any, has_example: bool, description: str
) -> tuple[str, str]:
    if has_example:
        return json.dumps(example_val, ensure_ascii=False), description

    if type_ is None:
        return "null", description

    defaults = {
        "string": '"string"',
        "boolean": "true",
        "integer": "0",
        "number": "0.0",
    }
    return defaults.get(type_, "null"), description


def generate_jsonc_sample_from_schema(
    schema: dict[str, Any],
    root_schema: dict[str, Any] | None = None,
    indent: int = 0,
) -> tuple[str, str]:
    if root_schema is None:
        root_schema = schema

    spaces = " " * indent
    description = schema.get("description", "")

    resolved_schema = _resolve_json_schema_reference(schema, root_schema)
    if resolved_schema is not None:
        resolved_val, resolved_desc = generate_jsonc_sample_from_schema(resolved_schema, root_schema, indent)
        return resolved_val, description or resolved_desc

    composite_schema = _select_first_composite_schema(schema)
    if composite_schema is not None:
        return generate_jsonc_sample_from_schema(composite_schema, root_schema, indent)

    type_ = schema.get("type")
    example_val, has_example = _get_schema_example(schema)

    if type_ == "object":
        return _render_object_jsonc_sample(
            schema,
            root_schema,
            spaces=spaces,
            indent=indent,
            description=description,
            example_val=example_val,
            has_example=has_example,
        )

    if type_ == "array":
        return _render_array_jsonc_sample(
            schema,
            root_schema,
            spaces=spaces,
            indent=indent,
            description=description,
            example_val=example_val,
            has_example=has_example,
        )

    return _render_scalar_jsonc_sample(type_, example_val, has_example, description)
