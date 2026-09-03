"""app/system/schema.py — System 专属请求/响应/视图模型（spec §6.7.1）。

复用 app.core.amp_api_schema 通用件；不重复定义 AMPFilter 等公共类型。
populate_by_name=True，camelCase alias 对外。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.amp_api_schema import AMPFilter, AMPPaginationRequest, AMPSortSpec, AMPTimeRange


class SystemEventQueryRequest(BaseModel):
    """POST /events/query 请求（spec §6.7.1，extends AMPQueryRequest + keyword）。"""

    model_config = ConfigDict(populate_by_name=True)

    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    keyword: str | None = None
    sort: list[AMPSortSpec] | None = None
    page: AMPPaginationRequest = Field(default_factory=AMPPaginationRequest)
    include_raw_log: bool = Field(default=False, alias="includeRawLog")


class SystemEventView(BaseModel):
    """events/query 单事件视图（spec §6.7.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    log_id: str = Field(alias="logId")
    timestamp: str
    aic: str
    severity_number: int = Field(alias="severityNumber")
    severity_text: str | None = Field(default=None, alias="severityText")
    trace_id: str | None = Field(default=None, alias="traceId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    message: str
    category: str | None = None
    component: str | None = None
    module: str | None = None
    tags: dict[str, str] | None = None
    raw_body: Any | None = Field(default=None, alias="rawBody")
