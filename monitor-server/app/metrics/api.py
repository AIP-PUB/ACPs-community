"""app/metrics/api.py — FastAPI 路由层（Metrics 模块）。

路由前缀 /metrics，api.instructions 规范：仅做请求解析与响应组装；
业务异常由 service / snapshot_service 抛出，全局 handler 转 Problem Details。
Profile 开关（analytics_enabled / governance_enabled）在路由器装配时条件化（§6.19）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from acps_sdk.oidc import HumanPrincipal
from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.core.amp_api_schema import AMPQueryResponse
from app.core.authz import apply_request_scope, require_operator, require_read
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.metrics import service, snapshot_service
from app.metrics.schema import (
    MetricsCapacityRequest,
    MetricsCapacitySaturationItem,
    MetricsRankingItem,
    MetricsRankingQueryRequest,
    MetricsSeries,
    MetricsSeriesQueryRequest,
    MetricsSLOEvaluateRequest,
    MetricsSLOEvaluateResponse,
    MetricsSnapshotQueryRequest,
    MetricsSnapshotView,
)

router = APIRouter(prefix="/metrics", tags=["Metrics"])
settings = get_settings()


async def get_redis_dep() -> AsyncGenerator[Redis]:
    """FastAPI 依赖：返回共享 Redis 客户端（同 Heartbeat 模式）。"""
    yield get_redis()


RedisDep = Annotated[Redis, Depends(get_redis_dep)]


# ── snapshots/query ───────────────────────────────────────────────────────────


@router.post(
    "/snapshots/query",
    status_code=200,
    summary="查询最新指标快照（Core）",
    responses={400: {}, 422: {}, 503: {}},
)
async def query_snapshots(
    request: MetricsSnapshotQueryRequest,
    redis: RedisDep,
    principal: HumanPrincipal | None = Depends(require_read("metrics:read")),
) -> AMPQueryResponse[MetricsSnapshotView]:
    """返回各 Agent 的最新指标快照（Redis 优先，TSDB exact-anchor 修复）。"""
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await snapshot_service.query_snapshots(redis, scoped_request)
    return AMPQueryResponse(items=items, meta=meta)


# ── series/query ──────────────────────────────────────────────────────────────


@router.post(
    "/series/query",
    status_code=200,
    summary="查询指标时序（Core）",
    responses={400: {}, 422: {}, 503: {}},
)
async def query_series(
    request: MetricsSeriesQueryRequest,
    principal: HumanPrincipal | None = Depends(require_read("metrics:read")),
) -> AMPQueryResponse[MetricsSeries]:
    """返回指定指标的时序数据，含自动 step 降级。"""
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
    items, meta = await service.query_series(scoped_request)
    return AMPQueryResponse(items=items, meta=meta)


# ── rankings/query（Analytics Profile） ───────────────────────────────────────


if settings.metrics_analytics_enabled:

    @router.post(
        "/rankings/query",
        status_code=200,
        summary="指标排行 TopN/BottomN（Analytics）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_rankings(
        request: MetricsRankingQueryRequest,
        principal: HumanPrincipal | None = Depends(require_read("metrics:read")),
    ) -> AMPQueryResponse[MetricsRankingItem]:
        """返回指标排行榜（TopN/BottomN），instant 查询，不分页。"""
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await service.query_rankings(scoped_request)
        return AMPQueryResponse(items=items, meta=meta)


# ── slo/evaluate + capacity/saturation（Governance Profile） ──────────────────


if settings.metrics_governance_enabled:

    @router.post(
        "/slo/evaluate",
        status_code=200,
        summary="SLO 批量评估（Governance）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def evaluate_slo(
        request: MetricsSLOEvaluateRequest,
        principal: HumanPrincipal | None = Depends(require_operator),
    ) -> MetricsSLOEvaluateResponse:
        """批量 SLO 评估，返回每条 rule × AIC 的 meets/breach 明细及 summary。"""
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        return await service.evaluate_slo(scoped_request)

    @router.post(
        "/capacity/saturation",
        status_code=200,
        summary="容量饱和度（Governance）",
        responses={400: {}, 422: {}, 503: {}},
    )
    async def query_capacity(
        request: MetricsCapacityRequest,
        principal: HumanPrincipal | None = Depends(require_operator),
    ) -> AMPQueryResponse[MetricsCapacitySaturationItem]:
        """两阶段容量饱和度分析，返回按饱和度降序排列的 TopN。"""
        scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field=None)
        items, meta = await service.query_capacity(scoped_request)
        return AMPQueryResponse(items=items, meta=meta)


__all__ = ["router"]
