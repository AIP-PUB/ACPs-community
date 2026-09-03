"""AMP API 通用请求与响应模型（Spec §6.1.2）。

本模块定义所有六种日志类型 API 共用的基础结构：
时间范围、过滤器、排序、分页、响应元信息等。

各日志类型的专属请求/响应模型（如 AuditRecordQueryRequest）分别定义在
对应模块的 schema.py 中，并导入此处的通用类型。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AMPFieldSampleCoverage(BaseModel):
    """单字段采样覆盖度信息（Spec §6.1.2）。

    用于 destinations/query 等端点的 sampleCoverage 字典值，
    描述某个 Nullable 指标字段的可用样本情况。
    """

    model_config = {"populate_by_name": True}

    available_samples: int = Field(alias="availableSamples", description="有效（非 NULL）样本数")
    total_samples: int = Field(alias="totalSamples", description="查询窗口内采样总数")
    coverage_ratio: float = Field(alias="coverageRatio", description="覆盖率 = available / total")
    status: Literal["complete", "partial", "unavailable"] = Field(description="覆盖状态")


class AMPTimeRange(BaseModel):
    """查询时间范围，左闭右开区间 [startAt, endAt)（Spec §6.1.2）。"""

    start_at: str = Field(alias="startAt", description="起始时间（含），ISO 8601 带时区")
    end_at: str = Field(alias="endAt", description="结束时间（不含），ISO 8601 带时区")

    model_config = {"populate_by_name": True}


class AMPFilterCondition(BaseModel):
    """单个字段过滤条件（Spec §6.1.2 AMPFilter）。"""

    field: str = Field(description="字段路径（点分隔），如 'body.actor.id'")
    op: str = Field(description="运算符：eq / ne / gt / gte / lt / lte / in / nin / contains / starts_with / is_null")
    value: Any = Field(default=None, description="匹配值")


class AMPFilter(BaseModel):
    """组合过滤器，支持任意嵌套（Spec §6.1.2 AMPFilter）。"""

    conditions: list[AMPFilterCondition] | None = None
    groups: list[AMPFilter] | None = None
    logic: Literal["and", "or", "not"] = "and"


class AMPSortSpec(BaseModel):
    """排序条件（Spec §6.1.2 AMPSortSpec）。"""

    field: str = Field(description="排序字段路径，取值受各 API 白名单约束")
    order: Literal["asc", "desc"] = "desc"


class AMPPaginationRequest(BaseModel):
    """游标分页请求（Spec §6.1.2 AMPPaginationRequest）。

    使用游标分页而非 offset，避免数据追加导致翻页错位。
    """

    limit: int = Field(default=50, ge=1, le=500, description="单页最大条数，默认 50，最大 500")
    cursor: str | None = Field(default=None, description="游标，来自上一页 meta.nextCursor")


class AMPResponseMeta(BaseModel):
    """查询响应元信息（Spec §6.1.2 AMPResponseMeta）。

    暴露数据新鲜度、分页游标与结果完整性。
    """

    data_freshness_at: str | None = Field(
        default=None,
        alias="dataFreshnessAt",
        description="当前读模型已处理到的事件时间水位（ISO 8601）；水位未知时省略",
    )
    ingestion_lag_ms: int | None = Field(
        default=None,
        alias="ingestionLagMs",
        description="估计消费滞后（毫秒）",
    )
    next_cursor: str | None = Field(default=None, alias="nextCursor", description="下一页游标，无更多数据时省略")
    approximate_total: int | None = Field(default=None, alias="approximateTotal", description="近似总量")
    partial: bool | None = Field(default=None, description="是否为部分结果（读模型滞后时）")
    elapsed_ms: int | None = Field(default=None, alias="elapsedMs", description="Provider 查询耗时（毫秒）")
    partial_data_fields: list[str] | None = Field(
        default=None,
        alias="partialDataFields",
        description="数据不完整的字段名列表（如 destinations/query 某 Nullable 指标全缺时）",
    )
    sample_coverage: dict[str, AMPFieldSampleCoverage] | None = Field(
        default=None,
        alias="sampleCoverage",
        description="各字段采样覆盖率详情（键为字段名 camelCase，值为 AMPFieldSampleCoverage）",
    )

    model_config = {"populate_by_name": True}


class AMPQueryResponse[T](BaseModel):
    """通用分页查询响应包络（Spec §6.1.2）。"""

    items: list[T]
    meta: AMPResponseMeta


class AMPTaskAccepted(BaseModel):
    """异步任务受理回执（202 响应体，Spec §6.1.1）。"""

    task_id: str = Field(alias="taskId", description="异步任务 ID")

    model_config = {"populate_by_name": True}
