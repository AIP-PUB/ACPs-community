"""统一 discovery API（支持 cpu/gpu 模式 + 转发）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 - FastAPI resolves dependency annotations at runtime

from app.core.database import get_async_session
from app.core.dependencies import ServiceRuntime, get_service_runtime
from app.discovery.schema import (
    DiscoveryRequest,
    DiscoveryResponse,
)
from app.discovery.service import (
    discover_request,
    get_available_agents_count_payload,
    get_database_stats_payload,
    get_discovery_health_payload,
    get_forwarder_status_payload,
)
from app.discovery.validator import validate_discovery_request
from app.heartbeat_sync.enrichment import attach_alive_status

router = APIRouter()


@router.post(
    "/discover",
    response_model=DiscoveryResponse,
    summary="发现 Agent",
    description="""
    DiscoveryRequest 字段说明【详细结构参考ACPs协议文档 -> ADP：智能体发现过程（ACPs-spec-ADP-v02.02）】：
- `type`：查询类型，explicit: 明确查询（默认）,
            exploratory: 探索性查询，用户没有明确目标；
            trending: 热门查询，返回当前流行的智能体；
            filtered: 过滤查询，只按 filter 过滤，query 被忽略
- `query`：自然语言查询文本
- `context`：上下文信息，可选
- `limit`：返回结果数量上限，
- `filter`：结构化过滤条件
- `forwardDepthLimit`：转发查询深度上限
- `forwardFanoutLimit`：单次转发扇出上限
- `forwardFanoutRemaining`：剩余允许转发次数
- `forwardChain`：转发链路记录
- `forwardTrustedServers`：可信转发服务列表
- `forwardSignatures`：转发签名列表
- `forwardEachTimeoutMs`：单次转发超时时间，单位毫秒
- `forwardTotalTimeoutMs`：总转发超时时间，单位毫秒
    """,
)
async def discover_endpoint(
    request: Annotated[
        DiscoveryRequest,
        Body(
            openapi_examples={
                "default": {
                    "summary": "查询示例：",
                    "value": {
                        "type": "explicit",
                        "query": "我要去北京旅行",
                        "limit": 5,
                        "filter": {
                            "conditions": [
                                {
                                    "field": "active",
                                    "op": "eq",
                                    "value": True,
                                }
                            ]
                        },
                    },
                }
            }
        ),
    ],
    runtime: Annotated[ServiceRuntime, Depends(get_service_runtime)],
) -> JSONResponse:
    validate_discovery_request(request)

    response = await discover_request(request, runtime=runtime)
    await attach_alive_status(response)
    return JSONResponse(content=response.to_dict(), status_code=200)


@router.get(
    "/health",
    summary="健康检查",
    description="用于检查发现服务是否正常运行",
)
async def health_check(runtime: Annotated[ServiceRuntime, Depends(get_service_runtime)]) -> JSONResponse:
    return JSONResponse(content=get_discovery_health_payload(runtime=runtime), status_code=200)


@router.get(
    "/stats",
    summary="获取数据库统计信息",
    description="获取数据库中 agents 和 skills 数量统计",
)
async def get_database_stats(
    session: Annotated[AsyncSession, Depends(get_async_session)],
    runtime: Annotated[ServiceRuntime, Depends(get_service_runtime)],
) -> JSONResponse:
    status_code, payload = await get_database_stats_payload(session=session, runtime=runtime)
    return JSONResponse(content=payload, status_code=status_code)


@router.get(
    "/available-agents-count",
    summary="获取可用智能体数量",
    description="返回缓存的可用智能体状态（定时刷新）",
)
async def get_available_agents_count(
    session: Annotated[AsyncSession, Depends(get_async_session)],
    runtime: Annotated[ServiceRuntime, Depends(get_service_runtime)],
) -> JSONResponse:
    status_code, payload = await get_available_agents_count_payload(session=session, runtime=runtime)
    return JSONResponse(content=payload, status_code=status_code)


@router.get(
    "/forwarder-status",
    summary="获取转发服务器状态",
    description="检查转发配置、健康状态和统计信息",
)
async def get_forwarder_status() -> dict[str, Any]:
    return await get_forwarder_status_payload()
