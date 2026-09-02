from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.agent.exception import AgentError, AgentErrorCode
from app.agent.model import Agent


def _service_module() -> Any:
    from app.agent import service

    return cast("Any", service)


def _require_loaded_agent(agent: Agent | None, agent_id: uuid.UUID) -> Agent:
    if agent is None:
        raise AgentError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AgentErrorCode.AGENT_NOT_FOUND,
            error_msg="Agent not found",
            input_params={"agent_id": str(agent_id)},
        )
    return agent


async def create_agent_async(session: AsyncSession, user_id: uuid.UUID, agent_data: dict[str, Any]) -> Agent:
    """创建新 Agent（异步请求路径）。"""
    service_module = _service_module()
    payload = dict(agent_data)
    service_module._sanitize_agent_write_payload(payload)

    name_owner_stmt = (
        select(Agent)
        .where(
            payload["name"] == service_module.AGENT_NAME_COL,
            service_module.AGENT_IS_ACTIVE_COL.is_(True),
            user_id != service_module.AGENT_CREATED_BY_ID_COL,
        )
        .limit(1)
    )
    name_owner_result = await session.execute(name_owner_stmt)
    name_owner = name_owner_result.scalar_one_or_none()
    if name_owner:
        raise service_module._build_name_already_claimed_error(payload["name"])

    existing_agent_stmt = (
        select(Agent)
        .where(
            payload["name"] == service_module.AGENT_NAME_COL,
            payload["version"] == service_module.AGENT_VERSION_COL,
            service_module.AGENT_IS_ACTIVE_COL.is_(True),
        )
        .limit(1)
    )
    existing_agent_result = await session.execute(existing_agent_stmt)
    existing_agent = existing_agent_result.scalar_one_or_none()
    if existing_agent:
        raise service_module._build_name_version_exists_error(payload["name"], payload["version"])

    service_module._normalize_agent_acs_payload(payload, error_name=AgentErrorCode.AGENT_CREATE_FAILED)

    current_time = service_module.get_beijing_time()
    agent = Agent(
        **payload,
        created_by_id=user_id,
        approval_status=service_module.ApprovalStatus.DRAFT,
        created_at=current_time,
        updated_at=current_time,
    )

    try:
        session.add(agent)
        await session.flush()
        return agent
    except SQLAlchemyError as exc:
        if "uq_agent_name_version" in str(exc):
            raise service_module._build_name_version_exists_error(payload["name"], payload["version"]) from None
        raise


def create_agent(db: Session, user_id: uuid.UUID, agent_data: dict[str, Any]) -> Agent:
    """创建处于草稿状态的新 Agent。"""
    service_module = _service_module()
    payload = dict(agent_data)
    service_module._sanitize_agent_write_payload(payload)

    name_owner = (
        db.query(Agent)
        .filter(
            payload["name"] == service_module.AGENT_NAME_COL,
            service_module.AGENT_IS_ACTIVE_COL.is_(True),
            user_id != service_module.AGENT_CREATED_BY_ID_COL,
        )
        .first()
    )
    if name_owner:
        raise service_module._build_name_already_claimed_error(payload["name"])

    existing_agent = (
        db.query(Agent)
        .filter(
            payload["name"] == service_module.AGENT_NAME_COL,
            payload["version"] == service_module.AGENT_VERSION_COL,
            service_module.AGENT_IS_ACTIVE_COL.is_(True),
        )
        .first()
    )
    if existing_agent:
        raise service_module._build_name_version_exists_error(payload["name"], payload["version"])

    service_module._normalize_agent_acs_payload(payload, error_name=AgentErrorCode.AGENT_CREATE_FAILED)

    current_time = service_module.get_beijing_time()
    agent = service_module.Agent(
        **payload,
        created_by_id=user_id,
        approval_status=service_module.ApprovalStatus.DRAFT,
        created_at=current_time,
        updated_at=current_time,
    )

    try:
        db.add(agent)
        db.flush()
        return cast("Agent", agent)
    except SQLAlchemyError as exc:
        if "uq_agent_name_version" in str(exc):
            raise service_module._build_name_version_exists_error(payload["name"], payload["version"]) from None
        raise


def update_agent(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID, agent_data: dict[str, Any]) -> Agent:
    """更新 Agent（仅允许草稿状态）。"""
    service_module = _service_module()
    agent = cast("Agent", service_module.get_agent(db, agent_id, raise_exception=True))
    payload = dict(agent_data)
    service_module._sanitize_agent_write_payload(payload)

    service_module._ensure_agent_is_editable(agent, agent_id, user_id)

    requested_identity = service_module._get_changed_agent_identity(payload, agent)
    if requested_identity is not None:
        new_name, new_version = requested_identity
        service_module._ensure_agent_identity_available(db, agent_id=agent_id, name=new_name, version=new_version)

    try:
        acs_updated = service_module._prepare_agent_update_payload(
            payload,
            current_acs_hash=agent.acs_hash,
            error_name=AgentErrorCode.AGENT_UPDATE_FAILED,
        )

        service_module._apply_agent_payload(agent, payload)
        agent.updated_at = service_module.get_beijing_time()

        service_module._sync_agent_update(db, agent, acs_updated=acs_updated)

        db.add(agent)
        db.flush()
        return agent
    except SQLAlchemyError as exc:
        if "uq_agent_name_version" in str(exc):
            name, version = service_module._get_target_agent_identity(payload, agent)
            raise service_module._build_name_version_exists_error(name, version) from None
        raise


async def update_agent_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_data: dict[str, Any],
) -> Agent:
    """更新 Agent（异步请求路径）。"""
    service_module = _service_module()
    payload = dict(agent_data)
    service_module._sanitize_agent_write_payload(payload)
    agent = cast(
        "Agent",
        await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True),
    )
    agent = _require_loaded_agent(agent, agent_id)

    service_module._ensure_agent_is_editable(agent, agent_id, user_id)

    requested_identity = service_module._get_changed_agent_identity(payload, agent)
    if requested_identity is not None:
        new_name, new_version = requested_identity
        await service_module._ensure_agent_identity_available_async(
            session,
            agent_id=agent_id,
            name=new_name,
            version=new_version,
        )

    try:
        acs_updated = service_module._prepare_agent_update_payload(
            payload,
            current_acs_hash=agent.acs_hash,
            error_name=AgentErrorCode.AGENT_UPDATE_FAILED,
        )

        service_module._apply_agent_payload(agent, payload)
        agent.updated_at = service_module.get_beijing_time()

        await service_module._sync_agent_update_async(session, agent, acs_updated=acs_updated)

        session.add(agent)
        await session.flush()
        return agent
    except SQLAlchemyError as exc:
        if "uq_agent_name_version" in str(exc):
            name, version = service_module._get_target_agent_identity(payload, agent)
            raise service_module._build_name_version_exists_error(name, version) from None
        raise


async def delete_agent_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str = "User deletion",
) -> bool:
    """删除 Agent（异步请求路径）。"""
    service_module = _service_module()
    agent = await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True)
    agent = _require_loaded_agent(agent, agent_id)

    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only delete your own agents",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )

    service_module._ensure_agent_can_be_deleted(agent, agent_id)

    current_time = service_module.get_beijing_time()
    agents_to_delete = [agent]

    ontology_prefix = service_module._get_derived_entity_like_prefix(agent)
    if ontology_prefix:
        derived_stmt = select(Agent).where(
            service_module.AGENT_AIC_COL.like(f"{ontology_prefix}%"),
            agent.id != service_module.AGENT_ID_COL,
            service_module.AGENT_IS_DELETED_COL.is_(False),
        )
        derived_result = await session.execute(derived_stmt)
        agents_to_delete.extend(list(derived_result.scalars().all()))

    for target_agent in agents_to_delete:
        service_module._mark_agent_deleted(target_agent, current_time=current_time, reason=reason)

        await service_module.update_agent_acs_data_async(target_agent, session)
        service_module.notify_ca_server_revoke_cert(target_agent, reason=5)
        session.add(target_agent)

    await session.flush()
    return True


async def submit_agent_for_approval_async(session: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """提交 Agent 进入审核（异步请求路径）。"""
    service_module = _service_module()
    agent = cast(
        "Agent",
        await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True),
    )
    agent = _require_loaded_agent(agent, agent_id)

    service_module._ensure_agent_is_transitionable_for_approval(agent, agent_id, action="submitted for review")
    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only submit your own agents for review",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )
    if agent.approval_status not in [service_module.ApprovalStatus.DRAFT, service_module.ApprovalStatus.REJECTED]:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only agents in draft or rejected status can be submitted for review",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )

    service_module._mark_agent_pending(agent)

    session.add(agent)
    await session.flush()
    return agent


async def cancel_agent_submission_async(session: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """撤销 Agent 提交（异步请求路径）。"""
    service_module = _service_module()
    agent = cast(
        "Agent",
        await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True),
    )
    agent = _require_loaded_agent(agent, agent_id)

    service_module._ensure_agent_is_transitionable_for_approval(agent, agent_id, action="canceled")
    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only cancel your own agent submissions",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )
    if agent.approval_status != service_module.ApprovalStatus.PENDING:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only agents in pending status can be canceled",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )

    service_module._mark_agent_draft(agent)

    session.add(agent)
    await session.flush()
    return agent


async def process_agent_approval_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    processor_id: uuid.UUID,
    approve: bool,
    comments: str | None = None,
) -> Agent:
    """处理 Agent 审核请求（异步请求路径）。"""
    service_module = _service_module()
    agent = cast(
        "Agent",
        await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True),
    )
    agent = _require_loaded_agent(agent, agent_id)

    service_module._ensure_agent_is_transitionable_for_approval(agent, agent_id, action="processed")

    processor = await service_module.get_user_async(session, processor_id, raise_exception=False)
    if not processor:
        raise AgentError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AgentErrorCode.PROCESSOR_NOT_FOUND,
            error_msg="Processor not found",
            input_params={"processor_id": str(processor_id)},
        )

    if not service_module._has_staff_processing_role(processor):
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.PROCESSOR_NOT_STAFF,
            error_msg="Only users with STAFF or ADMIN role can process agent approvals",
            input_params={"processor_id": str(processor_id)},
        )

    if agent.approval_status != service_module.ApprovalStatus.PENDING:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only agents in pending status can be processed",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )

    provider_updated = False
    if approve:
        provider_updated = await service_module.apply_verified_provider_snapshot_async(session, agent)

    service_module._mark_agent_processed(agent, processor_id=processor_id, approve=approve, comments=comments)

    session.add(agent)
    if approve and not agent.aic:
        await service_module.generate_aic_for_agent_async(session, agent)
    else:
        if approve and provider_updated and agent.aic:
            await service_module.update_agent_with_changelog_async(session, agent, {"acs": agent.acs})
        await session.flush()

    return agent


async def batch_delete_agents_async(
    session: AsyncSession,
    agent_ids: list[uuid.UUID],
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """批量删除 Agent（异步请求路径）。"""
    service_module = _service_module()
    results: dict[str, Any] = {"success": [], "failed": []}

    for agent_id in agent_ids:
        try:
            async with session.begin_nested():
                await service_module.delete_agent_async(session, agent_id, user_id, reason="Batch Delete")
            results["success"].append(str(agent_id))
        except AgentError as error:
            results["failed"].append({"id": str(agent_id), "reason": str(error)})

    return results


async def disable_agent_async(
    session: AsyncSession,
    agent_id: uuid.UUID,
    staff_user_id: uuid.UUID,
    reason: str = "Staff disable",
) -> Agent:
    """禁用 Agent（异步请求路径）。"""
    service_module = _service_module()
    agent = cast(
        "Agent",
        await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True),
    )
    agent = _require_loaded_agent(agent, agent_id)
    staff_user = await service_module.get_user_async(session, staff_user_id, raise_exception=False)

    if not staff_user:
        raise service_module._build_staff_user_not_found_error(staff_user_id)

    if not service_module._has_staff_processing_role(staff_user):
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.PROCESSOR_NOT_STAFF,
            error_msg="Only users with STAFF or ADMIN role can disable agents",
            input_params={"staff_user_id": str(staff_user_id)},
        )

    service_module._ensure_agent_can_be_disabled(agent, agent_id)

    current_time = service_module.get_beijing_time()
    agents_to_disable = [agent]

    ontology_prefix = service_module._get_derived_entity_like_prefix(agent)
    if ontology_prefix:
        derived_stmt = select(Agent).where(
            service_module.AGENT_AIC_COL.like(f"{ontology_prefix}%"),
            agent.id != service_module.AGENT_ID_COL,
            service_module.AGENT_IS_ACTIVE_COL.is_(True),
            service_module.AGENT_IS_DISABLED_COL.is_(False),
        )
        derived_result = await session.execute(derived_stmt)
        agents_to_disable.extend(list(derived_result.scalars().all()))

    for target_agent in agents_to_disable:
        service_module._mark_agent_disabled(target_agent, current_time=current_time, reason=reason)

        await service_module.update_agent_acs_data_async(target_agent, session)
        service_module.notify_ca_server_revoke_cert(target_agent, reason=5)
        session.add(target_agent)

    await session.flush()
    return agent


async def enable_agent_async(session: AsyncSession, agent_id: uuid.UUID, staff_user_id: uuid.UUID) -> Agent:
    """启用 Agent（异步请求路径）。"""
    service_module = _service_module()
    agent = cast(
        "Agent",
        await service_module.get_agent_async(session, agent_id, with_users=False, raise_exception=True),
    )
    agent = _require_loaded_agent(agent, agent_id)
    staff_user = await service_module.get_user_async(session, staff_user_id, raise_exception=False)

    if not staff_user:
        raise service_module._build_staff_user_not_found_error(staff_user_id)

    if not service_module._has_staff_processing_role(staff_user):
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.PROCESSOR_NOT_STAFF,
            error_msg="Only users with STAFF or ADMIN role can enable agents",
            input_params={"staff_user_id": str(staff_user_id)},
        )

    service_module._ensure_agent_can_be_enabled(agent, agent_id)

    current_time = service_module.get_beijing_time()
    agents_to_enable = [agent]

    ontology_prefix = service_module._get_derived_entity_like_prefix(agent)
    if ontology_prefix:
        derived_stmt = select(Agent).where(
            service_module.AGENT_AIC_COL.like(f"{ontology_prefix}%"),
            agent.id != service_module.AGENT_ID_COL,
            service_module.AGENT_IS_DISABLED_COL.is_(True),
        )
        derived_result = await session.execute(derived_stmt)
        agents_to_enable.extend(list(derived_result.scalars().all()))

    for target_agent in agents_to_enable:
        service_module._mark_agent_enabled(target_agent, current_time=current_time)

        await service_module.update_agent_acs_data_async(target_agent, session)
        session.add(target_agent)

    await session.flush()
    return agent


def delete_agent(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID, reason: str = "User deletion") -> bool:
    """删除 Agent（仅限所有者）。"""
    service_module = _service_module()
    agent = service_module.get_agent(db, agent_id, raise_exception=True)

    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only delete your own agents",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )

    service_module._ensure_agent_can_be_deleted(agent, agent_id)

    current_time = service_module.get_beijing_time()
    agents_to_delete = [agent]

    ontology_prefix = service_module._get_derived_entity_like_prefix(agent)
    if ontology_prefix:
        derived_entities = (
            db.query(Agent)
            .filter(
                service_module.AGENT_AIC_COL.like(f"{ontology_prefix}%"),
                agent.id != service_module.AGENT_ID_COL,
                service_module.AGENT_IS_DELETED_COL.is_(False),
            )
            .all()
        )
        agents_to_delete.extend(derived_entities)

    for target_agent in agents_to_delete:
        service_module._mark_agent_deleted(target_agent, current_time=current_time, reason=reason)

        service_module.update_agent_acs_data(target_agent, db)
        service_module.notify_ca_server_revoke_cert(target_agent, reason=5)
        db.add(target_agent)

    db.flush()
    return True


def submit_agent_for_approval(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """提交 Agent 进入审核流程。"""
    service_module = _service_module()
    agent = cast("Agent", service_module.get_agent(db, agent_id, raise_exception=True))

    service_module._ensure_agent_is_transitionable_for_approval(agent, agent_id, action="submitted for review")
    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only submit your own agents for review",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )

    if agent.approval_status not in [service_module.ApprovalStatus.DRAFT, service_module.ApprovalStatus.REJECTED]:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only agents in draft or rejected status can be submitted for review",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )

    service_module._mark_agent_pending(agent)

    db.add(agent)
    db.flush()

    return agent


def cancel_agent_submission(db: Session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """撤销处于待审核状态的 Agent 提交。"""
    service_module = _service_module()
    agent = cast("Agent", service_module.get_agent(db, agent_id, raise_exception=True))

    service_module._ensure_agent_is_transitionable_for_approval(agent, agent_id, action="canceled")
    if agent.created_by_id != user_id:
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
            error_msg="You can only cancel your own agent submissions",
            input_params={"agent_id": str(agent_id), "user_id": str(user_id)},
        )

    if agent.approval_status != service_module.ApprovalStatus.PENDING:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only agents in pending status can be canceled",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )

    service_module._mark_agent_draft(agent)

    db.add(agent)
    db.flush()

    return agent


def process_agent_approval(
    db: Session,
    agent_id: uuid.UUID,
    processor_id: uuid.UUID,
    approve: bool,
    comments: str | None = None,
) -> Agent:
    """处理 Agent 审核请求（通过或拒绝）。"""
    service_module = _service_module()
    agent = cast("Agent", service_module.get_agent(db, agent_id, raise_exception=True))

    service_module._ensure_agent_is_transitionable_for_approval(agent, agent_id, action="processed")

    processor = db.get(service_module.User, processor_id)
    if not processor:
        raise AgentError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AgentErrorCode.PROCESSOR_NOT_FOUND,
            error_msg="Processor not found",
            input_params={"processor_id": str(processor_id)},
        )

    if not service_module._has_staff_processing_role(processor):
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.PROCESSOR_NOT_STAFF,
            error_msg="Only users with STAFF or ADMIN role can process agent approvals",
            input_params={"processor_id": str(processor_id)},
        )

    if agent.approval_status != service_module.ApprovalStatus.PENDING:
        raise AgentError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AgentErrorCode.INVALID_STATUS_TRANSITION,
            error_msg="Only agents in pending status can be processed",
            input_params={"agent_id": str(agent_id), "status": agent.approval_status},
        )

    provider_updated = False
    if approve:
        provider_updated = service_module.apply_verified_provider_snapshot(db, agent)

    service_module._mark_agent_processed(agent, processor_id=processor_id, approve=approve, comments=comments)

    db.add(agent)
    if approve and not agent.aic:
        service_module.generate_aic_for_agent(db, agent)
    else:
        if approve and provider_updated and agent.aic:
            service_module.update_agent_with_changelog(db, agent, {"acs": agent.acs})
        db.flush()

    return agent


def batch_delete_agents(db: Session, agent_ids: list[uuid.UUID], user_id: uuid.UUID) -> dict[str, Any]:
    """批量删除多个 Agent。"""
    service_module = _service_module()
    results: dict[str, Any] = {"success": [], "failed": []}

    for agent_id in agent_ids:
        try:
            with db.begin_nested():
                service_module.delete_agent(db, agent_id, user_id, reason="Batch Delete")
            results["success"].append(str(agent_id))
        except AgentError as error:
            results["failed"].append({"id": str(agent_id), "reason": str(error)})

    return results


def disable_agent(
    db: Session,
    agent_id: uuid.UUID,
    staff_user_id: uuid.UUID,
    reason: str = "Staff disable",
) -> Agent:
    """禁用 Agent（仅工作人员）。"""
    service_module = _service_module()
    agent = cast("Agent", service_module.get_agent(db, agent_id, raise_exception=True))

    from app.account.model import User

    staff_user = db.get(User, staff_user_id)
    if not staff_user:
        raise service_module._build_staff_user_not_found_error(staff_user_id)

    if not service_module._has_staff_processing_role(staff_user):
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.PROCESSOR_NOT_STAFF,
            error_msg="Only users with STAFF or ADMIN role can disable agents",
            input_params={"staff_user_id": str(staff_user_id)},
        )

    service_module._ensure_agent_can_be_disabled(agent, agent_id)

    current_time = service_module.get_beijing_time()
    agents_to_disable = [agent]

    ontology_prefix = service_module._get_derived_entity_like_prefix(agent)
    if ontology_prefix:
        derived_entities = (
            db.query(Agent)
            .filter(
                service_module.AGENT_AIC_COL.like(f"{ontology_prefix}%"),
                agent.id != service_module.AGENT_ID_COL,
                service_module.AGENT_IS_ACTIVE_COL.is_(True),
                service_module.AGENT_IS_DISABLED_COL.is_(False),
            )
            .all()
        )
        agents_to_disable.extend(derived_entities)

    for target_agent in agents_to_disable:
        service_module._mark_agent_disabled(target_agent, current_time=current_time, reason=reason)

        service_module.update_agent_acs_data(target_agent, db)
        service_module.notify_ca_server_revoke_cert(target_agent, reason=5)
        db.add(target_agent)

    db.flush()
    return agent


def enable_agent(db: Session, agent_id: uuid.UUID, staff_user_id: uuid.UUID) -> Agent:
    """启用已禁用的 Agent（仅工作人员）。"""
    service_module = _service_module()
    agent = cast("Agent", service_module.get_agent(db, agent_id, raise_exception=True))

    from app.account.model import User

    staff_user = db.get(User, staff_user_id)
    if not staff_user:
        raise service_module._build_staff_user_not_found_error(staff_user_id)

    if not service_module._has_staff_processing_role(staff_user):
        raise AgentError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_name=AgentErrorCode.PROCESSOR_NOT_STAFF,
            error_msg="Only users with STAFF or ADMIN role can enable agents",
            input_params={"staff_user_id": str(staff_user_id)},
        )

    service_module._ensure_agent_can_be_enabled(agent, agent_id)

    current_time = service_module.get_beijing_time()
    agents_to_enable = [agent]

    ontology_prefix = service_module._get_derived_entity_like_prefix(agent)
    if ontology_prefix:
        derived_entities = (
            db.query(Agent)
            .filter(
                service_module.AGENT_AIC_COL.like(f"{ontology_prefix}%"),
                agent.id != service_module.AGENT_ID_COL,
                service_module.AGENT_IS_DISABLED_COL.is_(True),
            )
            .all()
        )
        agents_to_enable.extend(derived_entities)

    for target_agent in agents_to_enable:
        service_module._mark_agent_enabled(target_agent, current_time=current_time)

        service_module.update_agent_acs_data(target_agent, db)
        db.add(target_agent)

    db.flush()
    return agent
