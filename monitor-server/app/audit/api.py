"""Audit Query API — FastAPI 路由层。

提供 AMP-API-Design-Audit.md §6 / ACPs-spec-AMP.md §6.6.3 定义的八个端点。
前缀：/audit
"""

from __future__ import annotations

from typing import Annotated

import structlog
from acps_sdk.oidc import HumanPrincipal
from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service
from app.audit.schema import (
    AuditAggregateItem,
    AuditAggregateRequest,
    AuditChainAnchorView,
    AuditExportRequest,
    AuditExportTaskView,
    AuditIntegrityTaskView,
    AuditIntegrityVerifyRequest,
    AuditIntegrityVerifyResponse,
    AuditRecordQueryRequest,
    AuditRecordView,
)
from app.core.amp_api_schema import AMPQueryResponse, AMPTaskAccepted
from app.core.authz import apply_request_scope, ensure_path_aic_allowed, require_auditor, require_read
from app.core.db_session import get_async_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/audit", tags=["Audit"])

# FastAPI 依赖注入别名
SessionDep = Annotated[AsyncSession, Depends(get_async_session)]


@router.post(
    "/records/query",
    response_model=AMPQueryResponse[AuditRecordView],
    summary="批量查询审计记录",
    description="按条件检索在线审计记录。必须提供有界 timeRange。",
)
async def query_records(
    request: AuditRecordQueryRequest,
    session: SessionDep,
    principal: HumanPrincipal | None = Depends(require_read("audit:read")),
) -> AMPQueryResponse[AuditRecordView]:
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field="tenantId")
    items, meta = await service.query_records(session, scoped_request)
    return AMPQueryResponse(items=items, meta=meta)


@router.get(
    "/records/{audit_id}",
    summary="获取单条审计记录",
    description="按 auditId 精确获取单条记录，通过响应头 X-AMP-Data-Freshness-At 返回新鲜度。",
)
async def get_record(
    audit_id: str,
    session: SessionDep,
    response: Response,
    principal: HumanPrincipal | None = Depends(require_read("audit:read")),
) -> AuditRecordView:
    view, watermark = await service.get_record_by_id(session, audit_id)
    ensure_path_aic_allowed(view.aic, principal)
    response.headers["X-AMP-Data-Freshness-At"] = watermark
    return view


@router.post(
    "/integrity/verify",
    response_model=None,
    summary="提交完整性校验",
    description=("对指定记录或时间范围重新执行签名与链式校验。小范围同步返回结果，超过阈值返回 202 + taskId。"),
)
async def submit_integrity_verify(
    request: AuditIntegrityVerifyRequest,
    session: SessionDep,
    principal: HumanPrincipal | None = Depends(require_auditor("audit:verify")),
) -> AuditIntegrityVerifyResponse | JSONResponse:
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field="tenantId")
    result = await service.submit_integrity_verify(session, scoped_request)
    if isinstance(result, str):
        return JSONResponse(
            status_code=202,
            content=AMPTaskAccepted(task_id=result).model_dump(by_alias=True),
        )
    return result


@router.get(
    "/integrity/verify/{task_id}",
    response_model=AuditIntegrityTaskView,
    summary="查询完整性校验任务",
)
async def get_integrity_task(
    task_id: str,
    session: SessionDep,
    principal: HumanPrincipal | None = Depends(require_auditor("audit:verify")),
) -> AuditIntegrityTaskView:
    return await service.get_integrity_task(session, task_id)


@router.get(
    "/anchors/latest",
    response_model=AMPQueryResponse[AuditChainAnchorView],
    summary="查询最新链锚点",
    description="返回每条子链最新锚点。可用 chainId 精确过滤。",
)
async def get_latest_anchors(
    session: SessionDep,
    chain_id: str | None = None,
    principal: HumanPrincipal | None = Depends(require_read("audit:read")),
) -> AMPQueryResponse[AuditChainAnchorView]:
    items, meta = await service.get_latest_anchors(session, chain_id)
    return AMPQueryResponse(items=items, meta=meta)


@router.post(
    "/export",
    summary="提交导出任务",
    description="导出一律异步执行，返回 202 + taskId。",
    status_code=202,
    response_model=AMPTaskAccepted,
)
async def submit_export(
    request: AuditExportRequest,
    session: SessionDep,
    principal: HumanPrincipal | None = Depends(require_auditor("audit:export")),
) -> AMPTaskAccepted:
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field="tenantId")
    task_id = await service.submit_export(session, scoped_request)
    return AMPTaskAccepted(task_id=task_id)


@router.get(
    "/export/{task_id}",
    response_model=AuditExportTaskView,
    summary="查询导出任务",
)
async def get_export_task(
    task_id: str,
    session: SessionDep,
    principal: HumanPrincipal | None = Depends(require_auditor("audit:export")),
) -> AuditExportTaskView:
    return await service.get_export_task(session, task_id)


@router.post(
    "/summary/aggregate",
    response_model=AMPQueryResponse[AuditAggregateItem],
    summary="聚合统计",
    description="按指定维度做计数与时间范围汇总。必须提供有界 timeRange。",
)
async def aggregate_summary(
    request: AuditAggregateRequest,
    session: SessionDep,
    principal: HumanPrincipal | None = Depends(require_read("audit:read")),
) -> AMPQueryResponse[AuditAggregateItem]:
    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field="tenantId")
    items, meta = await service.aggregate_summary(session, scoped_request)
    return AMPQueryResponse(items=items, meta=meta)
