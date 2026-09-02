"""app/message/api.py — FastAPI 路由层（Message 模块，六端点，设计 §6.24）。

路由前缀 /message；仅做请求解析与响应组装。
业务异常由 service / lifecycle_service / destination_service 抛出，全局 handler 转 Problem Details。
Profile 开关在本文件条件化注册（与 access.api.py 同口径）：
  - Core（events/query）：恒注册
  - Reliability（lifecycles/* / deadletters）：message_reliability_enabled
  - Destination（destinations/throughput）：message_destination_enabled
  - State（destinations/query）：message_destination_enabled AND message_state_collector_enabled
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from acps_sdk.oidc import HumanPrincipal
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.amp_api_schema import AMPQueryResponse
from app.core.authz import (
    apply_request_scope,
    ensure_any_aic_allowed,
    require_operator,
    require_read,
)
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.message.schema import (
    MessageDeadletterQueryRequest,
    MessageDeadLetterView,
    MessageDestinationStateQueryRequest,
    MessageDestinationStateView,
    MessageEventView,
    MessageLifecycleQueryRequest,
    MessageLifecycleView,
    MessageQueryRequest,
    MessageThroughputRequest,
)

router = APIRouter(prefix="/message", tags=["Message"])
settings = get_settings()


async def _get_redis_dep() -> AsyncGenerator[Redis]:
    yield get_redis()


RedisDep = Annotated[Redis, Depends(_get_redis_dep)]


# ── Core Profile（恒注册）─────────────────────────────────────────────────────


@router.post(
    "/events/query",
    status_code=200,
    summary="查询原始 Message 事件（Core Profile）",
    responses={400: {}, 422: {}, 503: {}},
)
async def query_events(
    request: MessageQueryRequest,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("message:read")),
) -> AMPQueryResponse[MessageEventView]:
    from app.message import service

    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await service.query_events(redis, scoped_request)
    return AMPQueryResponse(items=items, meta=meta)


# ── Reliability Profile（message_reliability_enabled）──────────────────────────

if settings.message_reliability_enabled:

    @router.post(
        "/lifecycles/query",
        status_code=200,
        summary="查询 Message 生命周期聚合（Reliability Profile）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_lifecycles(
        request: MessageLifecycleQueryRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("message:read")),
    ) -> AMPQueryResponse[MessageLifecycleView]:
        from app.message import lifecycle_service

        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await lifecycle_service.query_lifecycles(redis, scoped_request)
        return AMPQueryResponse(items=items, meta=meta)

    @router.get(
        "/lifecycles/{message_id}",
        status_code=200,
        summary="获取 Message 生命周期详情（Reliability Profile，裸资源）",
        responses={404: {}, 422: {}, 503: {}},
    )
    async def get_lifecycle_by_message_id(
        message_id: str,
        redis: RedisDep,
        system: str | None = Query(default=None, description="过滤 system（可选）"),
        destination_name: str | None = Query(default=None, alias="destinationName", description="目的地名称"),
        destination_kind: str | None = Query(default=None, alias="destinationKind", description="目的地类型"),
        virtual_host: str | None = Query(default=None, alias="virtualHost", description="虚拟主机"),
        principal: HumanPrincipal | None = Depends(require_read("message:read")),
    ) -> JSONResponse:
        from app.message import lifecycle_service

        view, headers = await lifecycle_service.get_lifecycle_by_message_id(
            redis,
            message_id,
            system=system,
            destination_name=destination_name,
            destination_kind=destination_kind,
            virtual_host=virtual_host,
        )
        ensure_any_aic_allowed([*view.producer_aics, *view.consumer_aics], principal)
        return JSONResponse(
            content=view.model_dump(by_alias=True, exclude_none=True),
            headers=headers,
        )

    @router.post(
        "/deadletters/query",
        status_code=200,
        summary="查询死信消息（Reliability Profile）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_deadletters(
        request: MessageDeadletterQueryRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("message:read")),
    ) -> AMPQueryResponse[MessageDeadLetterView]:
        from app.message import lifecycle_service

        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await lifecycle_service.query_deadletters(redis, scoped_request)
        return AMPQueryResponse(items=items, meta=meta)


# ── Destination Profile（message_destination_enabled）────────────────────────

if settings.message_destination_enabled:

    @router.post(
        "/destinations/throughput",
        status_code=200,
        summary="查询目的地吞吐时序（Destination Profile，裸资源）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def get_throughput(
        request: MessageThroughputRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_operator),
    ) -> JSONResponse:
        from app.message import destination_service

        series, headers = await destination_service.get_throughput(redis, request)
        return JSONResponse(
            content=series.model_dump(by_alias=True, exclude_none=True, mode="json"),
            headers=headers,
        )


# ── Destination State（message_destination_enabled AND message_state_collector_enabled）

if settings.message_destination_enabled and settings.message_state_collector_enabled:

    @router.post(
        "/destinations/query",
        status_code=200,
        summary="查询目的地状态快照（Destination Profile）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_destination_states(
        request: MessageDestinationStateQueryRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_operator),
    ) -> AMPQueryResponse[MessageDestinationStateView]:
        from app.message import destination_service

        items, meta = await destination_service.query_destination_states(redis, request)
        return AMPQueryResponse(items=items, meta=meta)
