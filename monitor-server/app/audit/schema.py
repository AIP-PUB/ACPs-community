"""Audit Query API — Pydantic V2 请求与响应模型。

参照 ACPs-spec-AMP.md §6.6.1 及 AMP-API-Design-Audit.md §6。

通用 AMP API 类型（AMPTimeRange / AMPFilter 等）从 app.core.amp_api_schema 导入；
本文件只定义 Audit 专属的视图模型与请求模型。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.amp_api_schema import (
    AMPFilter,
    AMPPaginationRequest,
    AMPSortSpec,
    AMPTimeRange,
)

# ── Audit 视图模型 ─────────────────────────────────────────────────────────


class AuditRecordIntegrityView(BaseModel):
    """AuditRecordView 中的完整性校验信息。"""

    signature_alg: str = Field(alias="signatureAlg")
    signature_key_id: str = Field(alias="signatureKeyId")
    signature_verified: bool = Field(alias="signatureVerified")
    signature_checked_at: str = Field(alias="signatureCheckedAt")
    verification_failure_type: str | None = Field(
        default=None,
        alias="verificationFailureType",
    )
    previous_hash: str | None = Field(default=None, alias="previousHash")
    current_hash: str = Field(alias="currentHash")
    chain_verified: bool | None = Field(default=None, alias="chainVerified")
    chain_checked_at: str | None = Field(default=None, alias="chainCheckedAt")
    chain_anchor_id: str | None = Field(default=None, alias="chainAnchorId")

    model_config = {"populate_by_name": True}


class AuditBodyActorView(BaseModel):
    id: str
    type: str
    name: str | None = None
    role: str | None = None
    ip: str | None = None
    user_agent: str | None = Field(default=None, alias="userAgent")

    model_config = {"populate_by_name": True}


class AuditBodyActionView(BaseModel):
    name: str
    type: str
    method: str | None = None


class AuditBodyTargetView(BaseModel):
    type: str
    id: str
    name: str | None = None
    before: Any | None = None
    after: Any | None = None


class AuditBodyResultView(BaseModel):
    status: str
    reason: str | None = None
    error_code: str | None = Field(default=None, alias="errorCode")

    model_config = {"populate_by_name": True}


class AuditBodyView(BaseModel):
    actor: AuditBodyActorView
    action: AuditBodyActionView
    target: AuditBodyTargetView
    result: AuditBodyResultView


class AuditRecordView(BaseModel):
    """单条审计记录视图（GET /records/{auditId} 和 query 列表响应项）。"""

    audit_id: str = Field(alias="auditId")
    log_id: str = Field(alias="logId")
    timestamp: str
    aic: str
    trace_id: str | None = Field(default=None, alias="traceId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    chain_id: str = Field(alias="chainId")
    chain_seq: int = Field(alias="chainSeq")
    body: AuditBodyView
    integrity: AuditRecordIntegrityView

    model_config = {"populate_by_name": True}


class AuditChainAnchorView(BaseModel):
    """链锚定证据视图（GET /anchors/latest）。"""

    anchor_id: str = Field(alias="anchorId")
    chain_id: str = Field(alias="chainId")
    anchored_at: str = Field(alias="anchoredAt")
    last_audit_id: str = Field(alias="lastAuditId")
    last_chain_seq: int = Field(alias="lastChainSeq")
    last_current_hash: str = Field(alias="lastCurrentHash")
    anchor_method: str = Field(alias="anchorMethod")
    anchor_proof: Any = Field(alias="anchorProof")

    model_config = {"populate_by_name": True}


class AuditIntegrityFailure(BaseModel):
    """单条完整性校验失败明细。"""

    audit_id: str = Field(alias="auditId")
    failure_type: Literal["signature", "hash_chain", "missing_public_key", "storage_gap"] = Field(alias="failureType")
    detail: str

    model_config = {"populate_by_name": True}


class AuditIntegrityVerifySummary(BaseModel):
    checked_count: int = Field(alias="checkedCount")
    failed_count: int = Field(alias="failedCount")
    anchored_until: str | None = Field(default=None, alias="anchoredUntil")

    model_config = {"populate_by_name": True}


class AuditIntegrityVerifyResponse(BaseModel):
    """同步完整性校验响应。"""

    checked_at: str = Field(alias="checkedAt")
    summary: AuditIntegrityVerifySummary
    failures: list[AuditIntegrityFailure] = []

    model_config = {"populate_by_name": True}


class AuditIntegrityTaskView(BaseModel):
    """异步完整性校验任务视图（GET /integrity/verify/{taskId}）。"""

    task_id: str = Field(alias="taskId")
    status: Literal["pending", "running", "succeeded", "failed"]
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    summary: AuditIntegrityVerifySummary | None = None
    failures: list[AuditIntegrityFailure] | None = None
    error: str | None = None

    model_config = {"populate_by_name": True}


class AuditExportTaskView(BaseModel):
    """导出任务视图（GET /export/{taskId}）。"""

    task_id: str = Field(alias="taskId")
    status: Literal["pending", "running", "succeeded", "failed"]
    created_at: str = Field(alias="createdAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")
    record_count: int | None = Field(default=None, alias="recordCount")
    artifact_sha256: str | None = Field(default=None, alias="artifactSha256")
    download_url: str | None = Field(default=None, alias="downloadUrl")
    download_url_expires_at: str | None = Field(default=None, alias="downloadUrlExpiresAt")
    manifest_hash: str | None = Field(default=None, alias="manifestHash")
    error: str | None = None

    model_config = {"populate_by_name": True}


class AuditAggregateItem(BaseModel):
    """聚合统计结果项。"""

    group_key: dict[str, str] = Field(alias="groupKey")
    count: int
    first_seen_at: str = Field(alias="firstSeenAt")
    last_seen_at: str = Field(alias="lastSeenAt")

    model_config = {"populate_by_name": True}


# ── 请求模型 ──────────────────────────────────────────────────────────────


class AuditRecordQueryRequest(BaseModel):
    """POST /records/query 请求体。"""

    time_range: AMPTimeRange | None = Field(
        default=None, alias="timeRange", description="查询时间范围（必填，缺失时返回 400）"
    )
    filter: AMPFilter | None = None
    keyword: str | None = Field(default=None, description="受限关键词检索")
    sort: list[AMPSortSpec] | None = None
    page: AMPPaginationRequest | None = None
    include_raw_log: bool = Field(default=False, alias="includeRawLog")

    model_config = {"populate_by_name": True}


class AuditIntegrityVerifyRequest(BaseModel):
    """POST /integrity/verify 请求体。

    至少提供 recordIds、timeRange、filter 之一；带 filter 时必须同时带 timeRange。
    """

    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    record_ids: list[str] | None = Field(default=None, alias="recordIds")
    stop_on_first_failure: bool = Field(default=False, alias="stopOnFirstFailure")
    verify_anchor: bool = Field(default=False, alias="verifyAnchor")

    model_config = {"populate_by_name": True}


class AuditExportRequest(BaseModel):
    """POST /export 请求体。"""

    time_range: AMPTimeRange | None = Field(
        default=None, alias="timeRange", description="导出时间范围（必填，缺失时返回 400）"
    )
    filter: AMPFilter | None = None
    keyword: str | None = None
    format: Literal["ndjson", "parquet"] = "ndjson"
    include_raw: bool = Field(default=False, alias="includeRaw")
    signature_alg: Literal["EdDSA", "ES256"] | None = Field(default=None, alias="signatureAlg")

    model_config = {"populate_by_name": True}


_AGGREGATE_GROUP_BY_FIELDS: frozenset[str] = frozenset(
    {
        "body.actor.id",
        "body.actor.name",
        "body.actor.role",
        "body.action.type",
        "body.action.name",
        "body.target.type",
        "body.result.status",
        "body.result.errorCode",
        "integrity.signatureVerified",
        "integrity.chainVerified",
        "integrity.verificationFailureType",
        "integrity.signatureKeyId",
        "chainId",
    }
)


class AuditAggregateRequest(BaseModel):
    """POST /summary/aggregate 请求体。"""

    time_range: AMPTimeRange | None = Field(
        default=None, alias="timeRange", description="聚合时间范围（必填，缺失时返回 400）"
    )
    filter: AMPFilter | None = None
    keyword: str | None = None
    group_by: list[str] = Field(alias="groupBy", description="聚合分组维度，仅允许白名单字段")
    page: AMPPaginationRequest | None = None

    model_config = {"populate_by_name": True}

    @property
    def valid_group_by_fields(self) -> frozenset[str]:
        return _AGGREGATE_GROUP_BY_FIELDS
