from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.account.service_account import ensure_user_aic_provider_code, ensure_user_aic_provider_code_async
from app.agent.exception import AgentError, AgentErrorCode
from app.agent.model import ApprovalStatus
from app.core.config import settings
from app.sync.service import update_agent_with_changelog_async
from app.utils import aic
from app.utils.utils import get_beijing_time

if TYPE_CHECKING:
    from app.agent.model import Agent

type JsonObject = dict[str, object]
type JsonObjectList = list[JsonObject]
type AgentAcsUpdatePayload = dict[str, JsonObject]

AIC_URL_PLACEHOLDER = "{AIC}"


def _issue_agent_aic(agent: Agent, provider_code: str) -> str:
    """按当前配置签发 AIC。第5、6级与第8/9级长度来自 Settings，第7级来自账户字段。"""
    protocol_version = settings.aic_protocol_version
    manager_code = settings.aic_arsp_code
    if agent.is_ontology:
        return aic.generate_ontology_aic(
            protocol_version=protocol_version,
            manager_code=manager_code,
            provider_code=provider_code,
        )
    return aic.generate_aic(
        protocol_version=protocol_version,
        manager_code=manager_code,
        provider_code=provider_code,
    )


def _build_invalid_agent_acs_error(agent_id: uuid.UUID) -> AgentError:
    return AgentError(
        status_code=400,
        error_name=AgentErrorCode.INVALID_ACS,
        error_msg="Invalid ACS data format; must be a valid JSON object",
        input_params={"agent_id": str(agent_id)},
    )


def _load_agent_acs_data(agent: Agent) -> JsonObject:
    if isinstance(agent.acs, dict):
        return cast("JsonObject", agent.acs.copy())

    if not isinstance(agent.acs, str):
        raise _build_invalid_agent_acs_error(agent.id)

    try:
        acs_data = json.loads(agent.acs)
    except json.JSONDecodeError:
        raise _build_invalid_agent_acs_error(agent.id) from None

    if not isinstance(acs_data, dict):
        raise _build_invalid_agent_acs_error(agent.id)

    return cast("JsonObject", acs_data)


def _get_endpoint_objects(acs_data: JsonObject) -> JsonObjectList | None:
    raw_end_points = acs_data.get("endPoints")
    if not isinstance(raw_end_points, list):
        return None

    end_points: JsonObjectList = []
    for endpoint in raw_end_points:
        if isinstance(endpoint, dict):
            end_points.append(cast("JsonObject", endpoint))
    return end_points


def _replace_agent_aic_placeholders(acs_data: JsonObject, agent_aic: str | None) -> bool:
    end_points = _get_endpoint_objects(acs_data)
    if not agent_aic or end_points is None:
        return False

    changed = False
    for endpoint in end_points:
        endpoint_url = endpoint.get("url")
        if isinstance(endpoint_url, str) and AIC_URL_PLACEHOLDER in endpoint_url:
            endpoint["url"] = endpoint_url.replace(AIC_URL_PLACEHOLDER, agent_aic)
            changed = True

    return changed


def _prepare_agent_acs_update(agent: Agent) -> AgentAcsUpdatePayload | None:
    if not agent.acs:
        return None

    acs_data = _load_agent_acs_data(agent)
    is_acs_changed = False

    if acs_data.get("aic") != agent.aic:
        acs_data["aic"] = agent.aic
        is_acs_changed = True

    if acs_data.get("active") != agent.is_active:
        acs_data["active"] = agent.is_active
        is_acs_changed = True

    if _replace_agent_aic_placeholders(acs_data, agent.aic):
        is_acs_changed = True

    if not is_acs_changed:
        return None

    current_time = get_beijing_time()
    acs_data["lastModifiedTime"] = current_time.isoformat()
    agent.acs = acs_data
    return {"acs": agent.acs}


async def update_agent_acs_data_async(agent: Agent, session: AsyncSession | None = None) -> None:
    """异步更新 Agent 的 ACS 数据并在需要时写入 ChangeLog。"""
    agent_data = _prepare_agent_acs_update(agent)
    if agent_data is not None and session is not None:
        await update_agent_with_changelog_async(session, agent, agent_data)


def update_agent_acs_data(agent: Agent, db: Session | None = None) -> None:
    """同步更新 Agent 的 ACS 数据并在需要时写入 ChangeLog。"""
    agent_data = _prepare_agent_acs_update(agent)

    if agent_data is not None and db is not None:
        from app.sync.service import update_agent_with_changelog

        update_agent_with_changelog(db, agent, agent_data)


async def generate_aic_for_agent_async(session: AsyncSession, agent: Agent) -> Agent:
    """为已审批 Agent 在异步路径生成唯一 AIC。"""
    if agent.approval_status != ApprovalStatus.APPROVED:
        return agent
    if agent.aic:
        return agent

    provider_code = await ensure_user_aic_provider_code_async(session, agent.created_by_id)

    max_retries = 3
    retry_count = 0
    retry_delay = 0.002

    while retry_count < max_retries:
        try:
            async with session.begin_nested():
                agent.aic = _issue_agent_aic(agent, provider_code)
                agent.updated_at = get_beijing_time()

                await update_agent_acs_data_async(agent, session)

                session.add(agent)
                await session.flush()
            return agent
        except SQLAlchemyError:
            retry_count += 1
            if retry_count >= max_retries:
                raise
            await asyncio.sleep(retry_delay)

    return agent


def generate_aic_for_agent(db: Session, agent: Agent) -> Agent:
    """为已审批 Agent 在同步路径生成唯一 AIC。"""
    if agent.approval_status != ApprovalStatus.APPROVED:
        return agent
    if agent.aic:
        return agent

    provider_code = ensure_user_aic_provider_code(db, agent.created_by_id)

    max_retries = 3
    retry_count = 0
    retry_delay = 0.002

    while retry_count < max_retries:
        try:
            with db.begin_nested():
                agent.aic = _issue_agent_aic(agent, provider_code)
                agent.updated_at = get_beijing_time()

                update_agent_acs_data(agent, db)

                db.add(agent)
                db.flush()
            return agent
        except SQLAlchemyError as error:
            retry_count += 1
            if retry_count >= max_retries:
                raise error
            time.sleep(retry_delay)

    return agent
