"""app/system/api.py — FastAPI 路由（spec §6.7.3，单端点 events/query，Core Profile）。

路由前缀 /system；仅做请求解析与响应组装。
业务异常由 service 抛出，全局 handler 转 Problem Details（RFC 9457，error_code）。
system_query_enabled=false → 端点返回 404（只写部署，§7.2）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from acps_sdk.oidc import HumanPrincipal
from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis

from app.core.amp_api_schema import AMPQueryResponse
from app.core.authz import apply_request_scope, require_read
from app.core.redis_client import get_redis
from app.system import service
from app.system.schema import SystemEventQueryRequest, SystemEventView

router = APIRouter(prefix="/system", tags=["System"])


async def _get_redis_dep() -> AsyncGenerator[Redis]:
    yield get_redis()


RedisDep = Annotated[Redis, Depends(_get_redis_dep)]


@router.post(
    "/events/query",
    status_code=200,
    summary="检索系统日志事件（Core Profile）",
    responses={
        400: {"description": "AMP_INVALID_TIME_RANGE / AMP_CURSOR_INVALID"},
        404: {"description": "查询端点已禁用（system_query_enabled=false）"},
        422: {"description": "AMP_SYSTEM_KEYWORD_TOO_BROAD / AMP_OUT_OF_RETENTION / AMP_UNSUPPORTED_FIELD"},
        413: {"description": "AMP_RESULT_TOO_LARGE"},
        503: {"description": "AMP_READ_MODEL_LAGGING"},
    },
)
async def query_events(
    request: SystemEventQueryRequest,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("system:read")),
) -> AMPQueryResponse[SystemEventView]:
    """POST /acps-amp-v1/system/events/query（spec §6.7.3，Core Profile）。

    system_query_enabled=false 时返回 404（只写部署时关闭查询路径，§7.2）。
    principal 待鉴权中间件落地后注入（§12 O-1）。
    """
    from app.core.config import settings

    if not settings.system_query_enabled:
        raise HTTPException(status_code=404, detail="System query endpoint is disabled.")
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await service.query_events(redis, scoped_request, principal=principal)
    return AMPQueryResponse(items=items, meta=meta)
