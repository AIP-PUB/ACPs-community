"""app/access/api.py — FastAPI 路由层（Access 模块，7 端点）。

路由前缀 /access；仅做请求解析与响应组装。
业务异常由 service / trace_service / topology_service 抛出，全局 handler 转 Problem Details。
Profile 开关（analytics_enabled / apm_enabled）在装配时条件化（设计 §6.21、§10）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from acps_sdk.oidc import HumanPrincipal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.access import service, topology_service, trace_service
from app.access.schema import (
    AccessErrorAttribution,
    AccessErrorAttributionRequest,
    AccessEventView,
    AccessOperationQueryRequest,
    AccessOperationSummary,
    AccessQueryRequest,
    AccessSlowRequestItem,
    AccessSlowRequestRequest,
    AccessTopologyEdge,
    AccessTopologyQueryRequest,
    AccessTraceQueryRequest,
    AccessTraceSummary,
)
from app.core.amp_api_schema import AMPQueryResponse
from app.core.authz import (
    apply_request_scope,
    ensure_trace_view_allowed,
    principal_scope_filter,
    require_read,
)
from app.core.config import get_settings
from app.core.redis_client import get_redis

router = APIRouter(prefix="/access", tags=["Access"])
settings = get_settings()


async def _get_redis_dep() -> AsyncGenerator[Redis]:
    yield get_redis()


RedisDep = Annotated[Redis, Depends(_get_redis_dep)]


# ── Core Profile（恒注册）─────────────────────────────────────────────────────


@router.post(
    "/operations/query",
    status_code=200,
    summary="查询 Access 操作聚合（Core）",
    responses={400: {}, 422: {}, 503: {}},
)
async def query_operations(
    request: AccessOperationQueryRequest,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("access:read")),
) -> AMPQueryResponse[AccessOperationSummary]:
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await service.query_operations(redis, scoped_request)
    return AMPQueryResponse(items=items, meta=meta)


@router.post(
    "/events/query",
    status_code=200,
    summary="查询原始 Access 事件（Core）",
    responses={400: {}, 422: {}, 503: {}},
)
async def query_events(
    request: AccessQueryRequest,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("access:read")),
) -> AMPQueryResponse[AccessEventView]:
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await service.query_events(redis, scoped_request)
    return AMPQueryResponse(items=items, meta=meta)


# ── Analytics Profile（access_analytics_enabled）─────────────────────────────

if settings.access_analytics_enabled:

    @router.post(
        "/errors/attribution",
        status_code=200,
        summary="查询错误归因（Analytics）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_error_attribution(
        request: AccessErrorAttributionRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("access:read")),
    ) -> AMPQueryResponse[AccessErrorAttribution]:
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await service.query_error_attribution(redis, scoped_request)
        return AMPQueryResponse(items=items, meta=meta)

    @router.post(
        "/slow-requests/top",
        status_code=200,
        summary="查询慢请求 TopN（Analytics）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_slow_requests(
        request: AccessSlowRequestRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("access:read")),
    ) -> AMPQueryResponse[AccessSlowRequestItem]:
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await service.query_slow_requests(redis, scoped_request)
        return AMPQueryResponse(items=items, meta=meta)


# ── APM Profile（access_apm_enabled）──────────────────────────────────────────

if settings.access_apm_enabled:

    @router.post(
        "/traces/query",
        status_code=200,
        summary="查询 Trace 摘要列表（APM）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_traces(
        request: AccessTraceQueryRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("access:read")),
    ) -> AMPQueryResponse[AccessTraceSummary]:
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await trace_service.query_traces(redis, scoped_request)
        return AMPQueryResponse(items=items, meta=meta)

    @router.get(
        "/traces/{trace_id}",
        status_code=200,
        summary="获取 Trace 详情（APM，裸资源响应）",
        responses={404: {}, 503: {}},
    )
    async def get_trace(
        trace_id: str,
        redis: RedisDep,
        include_events: bool = Query(default=False, description="是否包含事件列表"),
        principal: HumanPrincipal | None = Depends(require_read("access:read")),
    ) -> JSONResponse:
        view, headers = await trace_service.get_trace(redis, trace_id, include_events=include_events)
        ensure_trace_view_allowed(view, principal)
        return JSONResponse(
            content=view.model_dump(by_alias=True, exclude_none=True),
            headers=headers,
        )

    @router.post(
        "/topology/query",
        status_code=200,
        summary="查询服务拓扑边（APM）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_topology(
        request: AccessTopologyQueryRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("access:read")),
    ) -> AMPQueryResponse[AccessTopologyEdge]:
        items, meta = await topology_service.query_topology(redis, request)
        scope = principal_scope_filter(principal)
        if not scope.is_admin and not scope.allowed_aics:
            raise HTTPException(
                status_code=403,
                detail="Request scope cannot be derived for this principal",
            )
        if not scope.is_admin and scope.allowed_aics:
            allowed = set(scope.allowed_aics)
            items = [item for item in items if {item.caller_aic, item.callee_aic} & allowed]
        return AMPQueryResponse(items=items, meta=meta)
