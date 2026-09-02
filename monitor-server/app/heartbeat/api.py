"""Heartbeat API — FastAPI 路由层（Query API §7.1.3 + Sync API §8.1.3）。

前缀：/heartbeat（由 app.include_router 添加 settings.api_v1_str 前缀）

Query 端点：
  GET  /liveness/{aic}           — 点查单 AIC liveness（§6.1.1）
  POST /liveness/query           — 列表查询（§6.1.2）
  POST /silence/top              — 静默排行（analytics_enabled 控制注册，§6.1.3）
  GET  /summary                  — 全局汇总统计（§6.1.4）

Sync 端点（8-10：即使 sync_enabled=False 也注册，由 ensure_sync_enabled() 返回 404）：
  GET  /sync/info                — Sync Profile 元信息（§6.1.5）
  GET  /sync/snapshot            — 全量 NDJSON 快照（§6.1.6）

7-8：get_redis_dep 定义在本文件（不在 redis_client.py，分工清晰）。
7-9：路由前缀由 settings.api_v1_str + "/heartbeat" 拼接。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

import structlog
from acps_sdk.amp.heartbeat_sync import HeartbeatSyncInfo
from acps_sdk.oidc import HumanPrincipal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.core.authz import (
    apply_request_scope,
    ensure_path_aic_allowed,
    principal_scope_filter,
    require_read,
    require_sync_access,
)
from app.core.config import settings
from app.core.redis_client import get_redis
from app.heartbeat import service, sync_service
from app.heartbeat.schema import (
    HeartbeatLivenessEnvelope,
    HeartbeatLivenessQueryRequest,
    HeartbeatQueryResponse,
    HeartbeatSilenceRankItem,
    HeartbeatSilenceTopRequest,
    HeartbeatSummaryEnvelope,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/heartbeat", tags=["Heartbeat"])


# 7-8：Redis 依赖注入（定义在 api.py，不在 redis_client.py）
async def get_redis_dep() -> AsyncGenerator[Redis]:
    """FastAPI 依赖：提供 Redis 客户端（进程级单例，不自持生命周期）。"""
    yield get_redis()


RedisDep = Annotated[Redis, Depends(get_redis_dep)]


# ── GET /liveness/{aic} ────────────────────────────────────────────────────────


@router.get(
    "/liveness/{aic}",
    response_model=HeartbeatLivenessEnvelope,
    summary="获取单个 AIC 的 liveness 状态",
    description="精确查询指定 AIC 的最后心跳时间与活跃状态。",
)
async def get_liveness(
    aic: str,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("heartbeat:read")),
) -> HeartbeatLivenessEnvelope:
    """GET /heartbeat/liveness/{aic}"""
    ensure_path_aic_allowed(aic, principal)
    view, meta = await service.get_liveness(redis, aic)
    return HeartbeatLivenessEnvelope(data=view, meta=meta)


# ── POST /liveness/query ───────────────────────────────────────────────────────


@router.post(
    "/liveness/query",
    response_model=HeartbeatQueryResponse[HeartbeatLivenessEnvelope],
    summary="批量查询 AIC liveness",
    description="按 aic in/eq 或 silence 时长区间查询，支持游标翻页。",
)
async def query_liveness(
    request: HeartbeatLivenessQueryRequest,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("heartbeat:read")),
) -> HeartbeatQueryResponse[HeartbeatLivenessEnvelope]:
    """POST /heartbeat/liveness/query"""
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await service.query_liveness(redis, scoped_request)
    # 将 view list 包装为 envelope list（response_model 要求）
    envelopes = [HeartbeatLivenessEnvelope(data=v, meta=meta) for v in items]
    return HeartbeatQueryResponse(items=envelopes, meta=meta)


# ── POST /silence/top ──────────────────────────────────────────────────────────
# analytics_enabled 控制是否注册（7-4：端点直接不存在，非 404）

if settings.heartbeat_analytics_enabled:

    @router.post(
        "/silence/top",
        response_model=HeartbeatQueryResponse[HeartbeatSilenceRankItem],
        summary="静默 AIC 排行",
        description="返回静默时间最长的 top N AIC（需 analyticsEnabled=true）。",
    )
    async def silence_top(
        request: HeartbeatSilenceTopRequest,
        redis: RedisDep,
        principal: HumanPrincipal | None = Depends(require_read("heartbeat:read")),
    ) -> HeartbeatQueryResponse[HeartbeatSilenceRankItem]:
        """POST /heartbeat/silence/top"""
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await service.silence_top(redis, scoped_request)
        return HeartbeatQueryResponse(items=items, meta=meta)


# ── GET /summary ───────────────────────────────────────────────────────────────


@router.get(
    "/summary",
    response_model=HeartbeatSummaryEnvelope,
    summary="全局 liveness 汇总",
    description="返回 alive/silent/total 计数与分桶统计。",
)
async def get_summary(
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("heartbeat:read")),
) -> HeartbeatSummaryEnvelope:
    """GET /heartbeat/summary"""
    scope = principal_scope_filter(principal)
    if not scope.is_admin and not scope.allowed_aics:
        raise HTTPException(
            status_code=403,
            detail="Request scope cannot be derived for this principal",
        )
    summary, meta = await service.get_summary(
        redis,
        allowed_aics=None if scope.is_admin else list(scope.allowed_aics),
    )
    return HeartbeatSummaryEnvelope(data=summary, meta=meta)


# ── GET /sync/info ─────────────────────────────────────────────────────────────
# 8-10: 即使 sync_enabled=False 也注册路由（ensure_sync_enabled() 返回 404 AMP 错误体）


@router.get(
    "/sync/info",
    response_model=HeartbeatSyncInfo,
    summary="Sync Profile 元信息",
    description="返回 alive-delta Sync Profile 的 topic、schema 版本、shard 数及当前 published_seq。",
)
async def get_sync_info(
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_sync_access),
) -> HeartbeatSyncInfo:
    """GET /heartbeat/sync/info"""
    sync_service.ensure_sync_enabled()
    return await sync_service.get_sync_info(redis)


# ── GET /sync/snapshot ─────────────────────────────────────────────────────────


@router.get(
    "/sync/snapshot",
    summary="全量 NDJSON 快照",
    description="流式返回全量 alive 集合快照（application/x-ndjson）。首行为 snapshot-meta，后续行为 upsert 条目。",
)
async def get_sync_snapshot(
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_sync_access),
) -> StreamingResponse:
    """GET /heartbeat/sync/snapshot"""
    from app.heartbeat.snapshot import get_snapshot_exporter

    sync_service.ensure_sync_enabled()
    exporter = get_snapshot_exporter()
    return StreamingResponse(
        sync_service.stream_snapshot(redis, exporter),
        media_type="application/x-ndjson",
    )
