"""app/access/schema.py — Access 专属请求/响应/视图模型（spec §6.4.1）。

复用 app.core.amp_api_schema 的通用件，定义 access 的 8 个视图 + 6 个请求模型。
全部 populate_by_name=True，camelCase alias 对外、snake_case 字段名对内。

数值字段精度约定（偏异 D-2）：
  - 原始事件字段（来自 CH UInt32/UInt16）→ int
  - 聚合/浮点统计字段（avg/quantiles/errorRate）→ float
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.amp_api_schema import AMPFilter, AMPPaginationRequest, AMPSortSpec, AMPTimeRange

# ── 请求基类与扩展 ────────────────────────────────────────────────────────────


class AccessQueryRequest(BaseModel):
    """events/query 请求基类（spec §6.4.3）。"""

    model_config = ConfigDict(populate_by_name=True)

    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    sort: list[AMPSortSpec] | None = None
    page: AMPPaginationRequest = Field(default_factory=AMPPaginationRequest)
    include_raw_log: bool = Field(default=False, alias="includeRawLog")


class AccessOperationQueryRequest(AccessQueryRequest):
    """operations/query 扩展请求（+groupBy/bucketSize/collapseBuckets/minRequestCount）。"""

    group_by: list[str] | None = Field(default=None, alias="groupBy")
    bucket_size: str | None = Field(default=None, alias="bucketSize")
    collapse_buckets: bool = Field(default=False, alias="collapseBuckets")
    min_request_count: int | None = Field(default=None, alias="minRequestCount")


class AccessTraceQueryRequest(AccessQueryRequest):
    """traces/query 扩展请求（+hasError/minTraceDurationMs/maxTraceDurationMs）。"""

    has_error: bool | None = Field(default=None, alias="hasError")
    min_trace_duration_ms: int | None = Field(default=None, alias="minTraceDurationMs")
    max_trace_duration_ms: int | None = Field(default=None, alias="maxTraceDurationMs")


class AccessTopologyQueryRequest(AccessQueryRequest):
    """topology/query 扩展请求（+groupBy/minCallCount/collapseBuckets）。"""

    group_by: str | None = Field(default=None, alias="groupBy")
    min_call_count: int | None = Field(default=None, alias="minCallCount")
    collapse_buckets: bool = Field(default=False, alias="collapseBuckets")


class AccessErrorAttributionRequest(AccessQueryRequest):
    """errors/attribution 扩展请求（+groupBy/topN）。"""

    group_by: list[str] | None = Field(default=None, alias="groupBy")
    top_n: int | None = Field(default=None, alias="topN")


class AccessSlowRequestRequest(AccessQueryRequest):
    """slow-requests/top 扩展请求（+topN/minDurationMs）。"""

    top_n: int | None = Field(default=None, alias="topN")
    min_duration_ms: int | None = Field(default=None, alias="minDurationMs")


# ── 视图子模型（Provider 侧响应契约，不复用 SDK 发射模型） ────────────────────────


class RequestInfo(BaseModel):
    """AccessEventView 中的请求信息子结构。"""

    model_config = ConfigDict(populate_by_name=True)

    method: str | None = None
    url: str | None = None
    route: str | None = None
    headers: dict[str, str] | None = None
    body_size_bytes: int | None = Field(default=None, alias="bodySizeBytes")


class ResponseInfo(BaseModel):
    """AccessEventView 中的响应信息子结构。"""

    model_config = ConfigDict(populate_by_name=True)

    status_code: int | None = Field(default=None, alias="statusCode")
    headers: dict[str, str] | None = None
    body_size_bytes: int | None = Field(default=None, alias="bodySizeBytes")


class CallerInfo(BaseModel):
    """调用方信息子结构。"""

    model_config = ConfigDict(populate_by_name=True)

    aic: str | None = None
    service_name: str | None = Field(default=None, alias="serviceName")
    ip: str | None = None


class CalleeInfo(BaseModel):
    """被调用方信息子结构。"""

    model_config = ConfigDict(populate_by_name=True)

    aic: str | None = None
    service_name: str | None = Field(default=None, alias="serviceName")
    ip: str | None = None


class AccessErrorInfo(BaseModel):
    """错误信息子结构。"""

    model_config = ConfigDict(populate_by_name=True)

    code: str | None = None
    message: str | None = None


# ── 视图モデル共通 timestamp 変換 ────────────────────────────────────────────


def _coerce_timestamp(v: Any) -> str:
    """ClickHouse から datetime として返される DateTime64 を ISO 文字列に変換する。"""
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


# ── 视图模型 ─────────────────────────────────────────────────────────────────


class AccessEventView(BaseModel):
    """access_events 行视图（events/query 响应体）。"""

    model_config = ConfigDict(populate_by_name=True)

    log_id: str = Field(alias="logId")
    timestamp: str
    aic: str
    trace_id: str = Field(alias="traceId")
    span_id: str = Field(alias="spanId")
    parent_span_id: str = Field(alias="parentSpanId")
    correlation_id: str = Field(alias="correlationId")
    severity: str
    duration_ms: int = Field(alias="durationMs")
    request_method: str = Field(alias="requestMethod")
    request_route: str = Field(alias="requestRoute")
    request_url: str = Field(alias="requestUrl")
    request_size: int = Field(alias="requestSize")
    response_status: int = Field(alias="responseStatus")
    response_size: int = Field(alias="responseSize")
    caller_aic: str = Field(alias="callerAic")
    caller_service: str = Field(alias="callerService")
    caller_ip: str = Field(alias="callerIp")
    callee_aic: str = Field(alias="calleeAic")
    callee_service: str = Field(alias="calleeService")
    callee_ip: str = Field(alias="calleeIp")
    error_code: str = Field(alias="errorCode")
    error_message: str = Field(alias="errorMessage")
    service_name: str = Field(alias="serviceName")
    deployment_env: str = Field(alias="deploymentEnv")
    request_headers: dict[str, str] = Field(alias="requestHeaders")
    response_headers: dict[str, str] = Field(alias="responseHeaders")
    attributes: dict[str, str]
    raw_log: str | None = Field(default=None, alias="rawLog")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_ts(cls, v: Any) -> str:
        return _coerce_timestamp(v)


class AccessTraceSpan(BaseModel):
    """access_trace_span 行视图（span 扁平投影）。"""

    model_config = ConfigDict(populate_by_name=True)

    log_id: str = Field(alias="logId")
    timestamp: str
    aic: str
    trace_id: str = Field(alias="traceId")
    span_id: str = Field(alias="spanId")
    parent_span_id: str = Field(alias="parentSpanId")
    duration_ms: int = Field(alias="durationMs")
    request_method: str = Field(alias="requestMethod")
    request_route: str = Field(alias="requestRoute")
    request_url: str | None = Field(default=None, alias="requestUrl")
    response_status: int = Field(alias="responseStatus")
    caller_aic: str = Field(alias="callerAic")
    callee_aic: str = Field(alias="calleeAic")
    error_code: str = Field(alias="errorCode")
    service_name: str = Field(alias="serviceName")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_ts(cls, v: Any) -> str:
        return _coerce_timestamp(v)


class AccessTraceSummary(BaseModel):
    """traces/query 响应体：trace 级摘要。"""

    model_config = ConfigDict(populate_by_name=True)

    trace_id: str = Field(alias="traceId")
    first_seen_at: str = Field(alias="firstSeenAt")
    last_seen_at: str = Field(alias="lastSeenAt")
    duration_ms: int = Field(alias="durationMs")
    total_spans: int = Field(alias="totalSpans")
    error_count: int = Field(alias="errorCount")
    root_aic: str | None = Field(default=None, alias="rootAic")
    root_endpoint: str | None = Field(default=None, alias="rootEndpoint")


class AccessTraceSummaryMeta(BaseModel):
    """AccessTraceView 内的 summary 子结构。"""

    model_config = ConfigDict(populate_by_name=True)

    first_seen_at: str = Field(alias="firstSeenAt")
    last_seen_at: str = Field(alias="lastSeenAt")
    duration_ms: int = Field(alias="durationMs")
    total_spans: int = Field(alias="totalSpans")
    error_count: int = Field(alias="errorCount")
    root_span_id: str | None = Field(default=None, alias="rootSpanId")
    root_aic: str | None = Field(default=None, alias="rootAic")
    root_endpoint: str | None = Field(default=None, alias="rootEndpoint")


class AccessTraceView(BaseModel):
    """traces/{traceId} 裸资源响应（无 meta，含 spans 重组与可选 events）。"""

    model_config = ConfigDict(populate_by_name=True)

    trace_id: str = Field(alias="traceId")
    spans: list[AccessTraceSpan]
    events: list[AccessEventView] | None = None
    summary: AccessTraceSummaryMeta | None = None


class AccessOperationSummary(BaseModel):
    """operations/query 响应体：端点维度聚合摘要。"""

    model_config = ConfigDict(populate_by_name=True)

    bucket: str | None = None
    dimensions: dict[str, str]
    request_count: int = Field(alias="requestCount")
    error_count: int = Field(alias="errorCount")
    error_rate: float = Field(alias="errorRate")
    avg_duration_ms: float = Field(alias="avgDurationMs")
    p95_duration_ms: float = Field(alias="p95DurationMs")
    p99_duration_ms: float = Field(alias="p99DurationMs")
    last_seen_at: str = Field(alias="lastSeenAt")


class AccessTopologyEdge(BaseModel):
    """topology/query 响应体：拓扑边（含 *Merge 聚合结果）。"""

    model_config = ConfigDict(populate_by_name=True)

    bucket: str | None = None
    grouped_by: str = Field(alias="groupedBy")
    caller_aic: str = Field(alias="callerAic")
    caller_service: str = Field(alias="callerService")
    callee_aic: str = Field(alias="calleeAic")
    callee_service: str = Field(alias="calleeService")
    call_count: int = Field(alias="callCount")
    error_count: int = Field(alias="errorCount")
    error_rate: float = Field(alias="errorRate")
    avg_duration_ms: float = Field(alias="avgDurationMs")
    p95_duration_ms: float = Field(alias="p95DurationMs")
    p99_duration_ms: float = Field(alias="p99DurationMs")
    last_seen_at: str = Field(alias="lastSeenAt")


class AccessErrorAttribution(BaseModel):
    """errors/attribution 响应体：错误归因条目。"""

    model_config = ConfigDict(populate_by_name=True)

    dimensions: dict[str, str]
    count: int
    error_message_sample: str | None = Field(default=None, alias="errorMessageSample")
    affected_aics: list[str] = Field(alias="affectedAics")
    affected_endpoints: list[tuple[str, str]] = Field(alias="affectedEndpoints")
    first_seen_at: str = Field(alias="firstSeenAt")
    last_seen_at: str = Field(alias="lastSeenAt")


class AccessSlowRequestItem(BaseModel):
    """slow-requests/top 响应体：单条慢请求。"""

    model_config = ConfigDict(populate_by_name=True)

    log_id: str = Field(alias="logId")
    timestamp: str
    aic: str
    trace_id: str | None = Field(default=None, alias="traceId")
    request_method: str | None = Field(default=None, alias="requestMethod")
    request_route: str | None = Field(default=None, alias="requestRoute")
    request_url: str | None = Field(default=None, alias="requestUrl")
    duration_ms: int = Field(alias="durationMs")
    response_status: int | None = Field(default=None, alias="responseStatus")
