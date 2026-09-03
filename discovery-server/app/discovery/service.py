from __future__ import annotations

import asyncio
import inspect
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING, Any

import httpx
from defusedxml import ElementTree
from fastapi import status
from openai import OpenAIError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.dependencies import ServiceRuntime, get_service_runtime
from app.core.logging_config import get_logger
from app.discovery.exception import ADPError, DiscoveryError, DiscoveryOperationError
from app.discovery.forwarder_config import get_config, get_stats, load_config, record_request
from app.discovery.schema import (
    DiscoveryAgentGroup,
    DiscoveryAgentSkill,
    DiscoveryFilters,
    DiscoveryRequest,
    DiscoveryResponse,
    DiscoveryResult,
    DiscoveryRoute,
    ErrorDetail,
    convert_filter_to_legacy,
)
from app.discovery.singleton import AgentDiscovery

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SyncFunc = Callable[..., Any]
AsyncFunc = Callable[..., Awaitable[Any]]
Func = Callable[..., Any]
WrappedReturn = tuple[Any, float]
type DiscoveryAgentRow = dict[str, Any]
DISCOVERY_SERVICE_BOUNDARY_ERRORS = (RuntimeError, ValueError, TypeError, OSError, SQLAlchemyError, OpenAIError)
FORWARDER_HEALTH_LOOP_ERRORS = (RuntimeError, ValueError, TypeError, OSError)

logger = get_logger(__name__)

_health_check_task: asyncio.Task[None] | None = None
_forwarder_healthy: bool = False
_last_health_check: float | None = None
AVAILABLE_AGENTS_STATUS_SQL = text(
    """
    SELECT aic, is_available, checked_at
    FROM available_agents_runtime
    ORDER BY aic
    """
)

# 模块加载时初始化转发配置
load_config()


def time_it_return_ms(func: Func) -> Callable[..., Awaitable[WrappedReturn]]:
    """记录同步或异步函数执行时间，并返回 (结果, 毫秒时间)。"""

    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> WrappedReturn:
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        end_time = time.perf_counter()
        return result, (end_time - start_time) * 1000

    @wraps(func)
    async def sync_wrapper_async_return(*args: Any, **kwargs: Any) -> WrappedReturn:
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        return result, (end_time - start_time) * 1000

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper_async_return


class DiscoveryService:
    """Agent 发现功能服务类。"""

    @time_it_return_ms
    async def discover_agents_async(
        self,
        request: str | DiscoveryRequest,
        mode: str = "gpu",
    ) -> list[DiscoveryAgentRow]:
        """精确查询。mode 用于区分 cpu/gpu 显式查询流程。"""

        try:
            if isinstance(request, str):
                query = request
                limit = 5
                legacy_filters: DiscoveryFilters | None = None
            else:
                query = request.query or ""
                limit = request.limit
                legacy_filters = convert_filter_to_legacy(request.filter)

            return await AgentDiscovery._discovery_agents_async(
                query=query,
                limit=limit,
                filters=legacy_filters,
                query_type=f"explicit_{mode}",
            )

        except DiscoveryOperationError:
            raise
        except DISCOVERY_SERVICE_BOUNDARY_ERRORS as exc:
            query_text = request if isinstance(request, str) else request.query
            raise DiscoveryOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=DiscoveryError.DISCOVERY_FAIL,
                error_msg=f"Failed to discover: {exc!s}",
                input_params={"query": query_text},
            ) from exc

    @time_it_return_ms
    async def filter_agents_async(self, filters: DiscoveryFilters, limit: int = 50) -> list[DiscoveryAgentRow]:
        """纯过滤查询。"""

        try:
            filtered_skills = await AgentDiscovery._filter_only_query(filters=filters, limit=limit)

            agent_response = []
            for ranking, skill in enumerate(filtered_skills, 1):
                agent_result = {
                    "acs": skill.get("acs", {}),
                    "skill_id": skill.get("skill_id", ""),
                    "ranking": ranking,
                    "memo": "Filtered query result",
                }
                agent_response.append(agent_result)
            return agent_response

        except DiscoveryOperationError:
            raise
        except DISCOVERY_SERVICE_BOUNDARY_ERRORS as exc:
            raise DiscoveryOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=DiscoveryError.DISCOVERY_FAIL,
                error_msg=f"Failed to filter agents: {exc!s}",
                input_params={"filters": str(filters)},
            ) from exc

    @time_it_return_ms
    async def discover_agents_trending(
        self,
        filters: DiscoveryFilters | None,
        limit: int = 5,
    ) -> list[DiscoveryAgentRow]:
        """trending 查询：复用过滤能力并随机打散返回。"""

        try:
            selected_filters = filters if filters is not None else DiscoveryFilters()
            pool_size = max(limit * 5, limit)
            filtered_skills = await AgentDiscovery._filter_only_query(filters=selected_filters, limit=pool_size)
            random.shuffle(filtered_skills)
            sampled = filtered_skills[:limit]

            agent_response = []
            for ranking, skill in enumerate(sampled, 1):
                agent_result = {
                    "acs": skill.get("acs", {}),
                    "skill_id": skill.get("skill_id", ""),
                    "ranking": ranking,
                    "memo": "trending",
                }
                agent_response.append(agent_result)
            return agent_response

        except DiscoveryOperationError:
            raise
        except DISCOVERY_SERVICE_BOUNDARY_ERRORS as exc:
            raise DiscoveryOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=DiscoveryError.DISCOVERY_FAIL,
                error_msg=f"Failed to query trending agents: {exc!s}",
                input_params={"limit": limit},
            ) from exc

    @time_it_return_ms
    async def decompose_task_llm_async(self, query: str) -> str:
        try:
            return await AgentDiscovery._call_llm_api(query)
        except DiscoveryOperationError:
            raise
        except DISCOVERY_SERVICE_BOUNDARY_ERRORS as exc:
            raise DiscoveryOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=DiscoveryError.DISCOVERY_FAIL,
                error_msg=f"Failed to decompose query: {exc!s}",
                input_params={"query": query},
            ) from exc


# 全局服务实例
discovery_service = DiscoveryService()


def _resolve_runtime(runtime: ServiceRuntime | None = None) -> ServiceRuntime:
    return runtime if runtime is not None else get_service_runtime()


def _current_mode(runtime: ServiceRuntime | None = None) -> str:
    return (_resolve_runtime(runtime).settings.DISCOVERY_MODE or "cpu").strip().lower()


def _extract_score_from_memo(memo: str) -> float | None:
    try:
        if "Rerank分数:" in memo:
            return float(memo.split("Rerank分数:")[1].strip().split()[0])
        if "RRF分数:" in memo:
            return float(memo.split("RRF分数:")[1].strip().split()[0])
    except IndexError, ValueError:
        return None
    return None


def _build_group(
    group_name: str,
    agent_rows: list[DiscoveryAgentRow],
    acs_map: dict[str, dict[str, Any]],
) -> DiscoveryAgentGroup:
    skills: list[DiscoveryAgentSkill] = []
    for default_rank, row in enumerate(agent_rows, 1):
        acs = row.get("acs") or {}
        aic = acs.get("aic") or row.get("aic") or ""
        if not aic:
            continue
        acs_map[aic] = acs

        skill_id = row.get("skillId") or row.get("skill_id") or ""
        ranking = int(row.get("ranking") or default_rank)
        memo = row.get("memo") or ""

        skills.append(
            DiscoveryAgentSkill(
                aic=aic,
                skill_id=skill_id,
                ranking=ranking,
                memo=memo,
            )
        )

    return DiscoveryAgentGroup(group=group_name, agent_skills=skills)


def _build_response(
    *,
    request: DiscoveryRequest,
    groups: list[DiscoveryAgentGroup],
    acs_map: dict[str, dict[str, Any]],
    duration_ms: float,
) -> DiscoveryResponse:
    route = DiscoveryRoute(
        forward_chain=request.forwardChain or ["AIC-DS-A"],
        agent_groups=groups,
        status="ok",
        duration_ms=int(duration_ms),
    )
    result = DiscoveryResult(acs_map=acs_map, agents=groups, routes=[route])
    result._alive_enrichable = True  # 标记为本地产出（§8.1），供 attach_alive_status 注入 aliveMap
    return DiscoveryResponse.success(result=result)


def _build_single_group_response(
    request: DiscoveryRequest,
    group_name: str,
    rows: list[DiscoveryAgentRow],
    duration_ms: float,
) -> DiscoveryResponse:
    acs_map: dict[str, dict[str, Any]] = {}
    groups = [_build_group(group_name, rows, acs_map)]
    return _build_response(request=request, groups=groups, acs_map=acs_map, duration_ms=duration_ms)


def _should_fallback_to_exploratory(rows: list[DiscoveryAgentRow], threshold: float) -> bool:
    if not rows:
        return True

    first_score = _extract_score_from_memo(rows[0].get("memo", ""))
    return first_score is not None and first_score < threshold


async def _handle_filtered_discovery(request: DiscoveryRequest) -> DiscoveryResponse:
    legacy_filters = convert_filter_to_legacy(request.filter)
    if legacy_filters is None:
        raise ADPError(ErrorDetail(code=40000, message="BadRequest", data="filtered 查询必须提供 filter 参数"))

    rows, duration_ms = await discovery_service.filter_agents_async(
        filters=legacy_filters,
        limit=request.limit,
    )
    return _build_single_group_response(request, request.query or "filtered", rows, duration_ms)


async def _handle_trending_discovery(request: DiscoveryRequest) -> DiscoveryResponse:
    legacy_filters = convert_filter_to_legacy(request.filter)
    rows, duration_ms = await discovery_service.discover_agents_trending(
        filters=legacy_filters,
        limit=request.limit,
    )
    return _build_single_group_response(request, "trending", rows, duration_ms)


async def _handle_exploratory_discovery(request: DiscoveryRequest, mode: str) -> DiscoveryResponse:
    if mode != "gpu":
        raise ADPError(
            ErrorDetail(
                code=40005,
                message="UnsupportedQueryType",
                data="exploratory 查询仅在 DISCOVERY_MODE=gpu 时可用",
            )
        )

    groups, acs_map, duration_ms = await discovery_exploratory(request)
    return _build_response(request=request, groups=groups, acs_map=acs_map, duration_ms=duration_ms)


def parse_task_xml(response_text: str) -> list[str]:
    match = re.search(r"(<task>.*?</task>)", response_text, re.DOTALL)
    if not match:
        return []

    xml_string = match.group(1)
    try:
        root = ElementTree.fromstring(xml_string)
    except ElementTree.ParseError:
        return []

    tasks: list[str] = []
    for child in root:
        text = child.text.strip() if child.text else ""
        if text:
            tasks.append(text)
    return tasks


async def check_forwarder_health() -> bool:
    config = get_config()
    if not config.forwarder_server_enabled or not config.forwarder_server_url:
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            health_url = f"{config.forwarder_server_url}/health"
            response = await client.get(health_url)
            if response.status_code != 200:
                return False
            data = response.json()
            return isinstance(data, dict) and data.get("status") == "healthy"
    except httpx.HTTPError, ValueError:
        return False


async def _periodic_forwarder_health_check() -> None:
    global _forwarder_healthy, _last_health_check

    while True:
        try:
            config = get_config()
            if config.forwarder_server_enabled:
                _forwarder_healthy = await check_forwarder_health()
                _last_health_check = time.time()
            else:
                _forwarder_healthy = False
        except asyncio.CancelledError:
            raise
        except FORWARDER_HEALTH_LOOP_ERRORS as exc:
            logger.warning("转发健康检查循环失败，回退为不可用状态", error=str(exc))
            _forwarder_healthy = False
        await asyncio.sleep(get_config().forwarder_health_check_interval)


def get_forwarder_health_status() -> bool:
    return _forwarder_healthy if get_config().forwarder_server_enabled else False


def start_health_check_task() -> None:
    global _health_check_task

    config = get_config()
    if not config.forwarder_server_enabled or _health_check_task is not None:
        return

    _health_check_task = asyncio.create_task(_periodic_forwarder_health_check())
    logger.info("转发服务器健康检查任务已启动")


async def stop_health_check_task() -> None:
    global _health_check_task

    if _health_check_task is None:
        return

    _health_check_task.cancel()
    with suppress(asyncio.CancelledError):
        await _health_check_task
    _health_check_task = None


async def forward_to_forwarder(request: DiscoveryRequest) -> DiscoveryResponse | None:
    config = get_config()
    if not config.forwarder_server_enabled or not config.forwarder_server_url:
        return None

    retries = config.forwarder_request_retries
    request_data = request.model_dump(by_alias=True, exclude_none=True)

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=config.forwarder_server_timeout) as client:
                endpoint = f"{config.forwarder_server_url}/discover"
                response = await client.post(
                    endpoint,
                    json=request_data,
                    headers={"accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                return DiscoveryResponse.from_dict(data)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "转发请求失败",
                attempt=attempt + 1,
                max_attempts=retries + 1,
                error=str(exc),
            )
            if attempt >= retries:
                return None

    return None


async def discovery_exploratory(
    request: DiscoveryRequest,
) -> tuple[list[DiscoveryAgentGroup], dict[str, dict[str, Any]], float]:
    response_text, duration_ms = await discovery_service.decompose_task_llm_async(request.query or "")
    cleaned = response_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    subtasks = parse_task_xml(cleaned.strip())
    groups: list[DiscoveryAgentGroup] = []
    acs_map: dict[str, dict[str, Any]] = {}

    if not subtasks:
        return groups, acs_map, duration_ms

    for subtask in subtasks:
        sub_request = request.model_copy(update={"type": "explicit", "query": subtask})
        rows, sub_duration = await discovery_service.discover_agents_async(sub_request, mode="gpu")
        duration_ms += sub_duration
        groups.append(_build_group(subtask, rows, acs_map))

    return groups, acs_map, duration_ms


async def local_discover(
    request: DiscoveryRequest,
    *,
    runtime: ServiceRuntime | None = None,
) -> DiscoveryResponse:
    mode = _current_mode(runtime)
    auto_fallback_threshold = 0.06

    if request.type == "filtered":
        return await _handle_filtered_discovery(request)

    if request.type == "trending":
        return await _handle_trending_discovery(request)

    if request.type == "exploratory":
        return await _handle_exploratory_discovery(request, mode)

    rows, duration_ms = await discovery_service.discover_agents_async(request, mode=mode)

    if mode == "gpu" and _should_fallback_to_exploratory(rows, auto_fallback_threshold):
        exploratory_request = request.model_copy(update={"type": "exploratory"})
        groups, acs_map, exploratory_ms = await discovery_exploratory(exploratory_request)
        duration_ms += exploratory_ms
        return _build_response(request=request, groups=groups, acs_map=acs_map, duration_ms=duration_ms)

    return _build_single_group_response(request, request.query or "", rows, duration_ms)


async def discover_request(
    request: DiscoveryRequest,
    *,
    runtime: ServiceRuntime | None = None,
) -> DiscoveryResponse:
    config = get_config()
    used_forwarder = False
    fallback_to_local = False
    can_forward = request.type not in ("filtered", "trending")

    if can_forward and config.forwarder_server_enabled:
        if not get_forwarder_health_status():
            logger.info("转发服务器当前不可用")

            if not config.forwarder_fallback_to_local:
                record_request(used_forwarder=True, success=False)
                raise ADPError(
                    ErrorDetail(
                        code=50301,
                        message="ForwarderUnavailable",
                        data="转发服务器不可用，且已禁用回退",
                    )
                )

            used_forwarder = True
            fallback_to_local = True
            logger.info("转发服务器不可用，回退本地处理")
        else:
            logger.info("尝试使用转发服务器处理请求")
            used_forwarder = True
            forwarded = await forward_to_forwarder(request)
            if forwarded is not None:
                logger.info("使用转发服务器响应")
                record_request(used_forwarder=True, success=True)
                return forwarded

            logger.info("转发服务器处理失败")

            if not config.forwarder_fallback_to_local:
                record_request(used_forwarder=True, success=False)
                raise ADPError(
                    ErrorDetail(
                        code=50301,
                        message="ForwarderUnavailable",
                        data="转发服务器不可用，且已禁用回退",
                    )
                )

            fallback_to_local = True
            logger.info("回退本地处理")

    response = await local_discover(request, runtime=runtime)

    if used_forwarder:
        record_request(used_forwarder=True, success=False)
        if fallback_to_local:
            logger.info("本地回退处理完成")
    else:
        record_request(used_forwarder=False, success=True)

    return response


def get_discovery_health_payload(runtime: ServiceRuntime | None = None) -> dict[str, Any]:
    resolved_runtime = _resolve_runtime(runtime)
    return {
        "status": "healthy",
        "service": "discovery-unified",
        "timestamp": datetime.now(UTC).isoformat(),
        "version": resolved_runtime.settings.APP_VERSION,
        "mode": _current_mode(resolved_runtime),
        "forwarderHealthy": get_forwarder_health_status(),
    }


async def get_database_stats_payload(
    *,
    session: AsyncSession | None = None,
    runtime: ServiceRuntime | None = None,
) -> tuple[int, dict[str, Any]]:
    try:
        from sqlmodel import func, select

        from app.sync.model import Agent, Skill

        if session is None:
            async with _resolve_runtime(runtime).session_factory() as owned_session:
                agents_count_result = await owned_session.execute(select(func.count()).select_from(Agent))
                skills_count_result = await owned_session.execute(select(func.count()).select_from(Skill))
        else:
            agents_count_result = await session.execute(select(func.count()).select_from(Agent))
            skills_count_result = await session.execute(select(func.count()).select_from(Skill))

        return 200, {
            "status": "ok",
            "data": {
                "agents": agents_count_result.scalar(),
                "skills": skills_count_result.scalar(),
            },
            "timestamp": datetime.now(UTC).isoformat(),
            "server_type": _current_mode(runtime),
        }
    except SQLAlchemyError as exc:
        logger.exception("获取数据库统计信息失败", error=str(exc))
        return 500, {"status": "error", "error": str(exc)}


async def get_available_agents_count_payload(
    *,
    session: AsyncSession | None = None,
    runtime: ServiceRuntime | None = None,
) -> tuple[int, dict[str, Any]]:
    try:
        if session is None:
            async with _resolve_runtime(runtime).session_factory() as owned_session:
                result = await owned_session.execute(AVAILABLE_AGENTS_STATUS_SQL)
                rows = result.mappings().all()
        else:
            result = await session.execute(AVAILABLE_AGENTS_STATUS_SQL)
            rows = result.mappings().all()

        available_aics = [row["aic"] for row in rows if row["is_available"]]
        last_updated = max((row["checked_at"] for row in rows if row["checked_at"] is not None), default=None)

        return 200, {
            "status": "ok",
            "data": {
                "total_active_agents": len(rows),
                "available_agents": len(available_aics),
                "available_aics": available_aics,
                "last_updated": last_updated.isoformat() if last_updated else None,
            },
            "server_type": _current_mode(runtime),
        }
    except SQLAlchemyError as exc:
        logger.exception("获取可用智能体缓存失败", error=str(exc))
        return 500, {"status": "error", "error": str(exc)}


async def get_forwarder_status_payload() -> dict[str, Any]:
    global _forwarder_healthy, _last_health_check

    config = get_config()
    if config.forwarder_server_enabled:
        _forwarder_healthy = await check_forwarder_health()
        _last_health_check = time.time()
    else:
        _forwarder_healthy = False

    stats = get_stats()
    forwarder_status = "not_configured"
    if config.forwarder_server_enabled:
        forwarder_status = "available" if _forwarder_healthy else "configured_but_unavailable"

    return {
        "enabled": config.forwarder_server_enabled,
        "url": config.forwarder_server_url if config.forwarder_server_enabled else None,
        "timeout": config.forwarder_server_timeout,
        "healthy": _forwarder_healthy,
        "last_check_time": datetime.fromtimestamp(_last_health_check, UTC).isoformat() if _last_health_check else None,
        "check_interval": config.forwarder_health_check_interval,
        "fallback_to_local": config.forwarder_fallback_to_local,
        "retries": config.forwarder_request_retries,
        "stats": {
            "total_requests": stats.total_requests,
            "forwarder_requests": stats.forwarder_requests,
            "forwarder_success": stats.forwarder_success,
            "forwarder_failures": stats.forwarder_failures,
            "local_fallback": stats.local_fallback,
            "forwarder_success_rate": stats.forwarder_success_rate,
            "forwarder_usage_rate": stats.forwarder_usage_rate,
        },
        "status": forwarder_status,
    }
