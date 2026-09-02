"""app/metrics/schema.py — Metrics 专属 Pydantic 请求 / 响应 / 视图模型。

通用类型从 app.core.amp_api_schema 导入；SDK 线缆模型（LoadMetrics / WindowMetrics）
从 acps_sdk.amp.models 导入——均不在此处重复定义或 re-export（schema.instructions）。

外部字段 camelCase alias、populate_by_name=True。
数值字段用 float（偏异 D-2）。
"""

from __future__ import annotations

from typing import Literal

from acps_sdk.amp.models import LoadMetrics, WindowMetrics
from pydantic import BaseModel, Field

from app.core.amp_api_schema import (
    AMPFilter,
    AMPPaginationRequest,
    AMPResponseMeta,
    AMPSortSpec,
    AMPTimeRange,
)

__all__ = [
    "MetricSeriesPoint",
    "MetricsCapacityRequest",
    "MetricsCapacitySaturationItem",
    "MetricsRankingItem",
    "MetricsRankingQueryRequest",
    "MetricsSLOEvaluateRequest",
    "MetricsSLOEvaluateResponse",
    "MetricsSLOEvaluation",
    "MetricsSLORule",
    "MetricsSLOSummary",
    "MetricsSeries",
    "MetricsSeriesQueryRequest",
    "MetricsSnapshotQueryRequest",
    "MetricsSnapshotView",
]


# ── 资源视图模型（spec §6.3.1） ────────────────────────────────────────────────


class MetricsSnapshotView(BaseModel):
    """单个 Agent 最新快照视图（snapshots/query 条目）。"""

    model_config = {"populate_by_name": True}

    aic: str
    observed_at: str = Field(alias="observedAt", description="最新快照事件时间（ISO 8601）")
    uptime_seconds: float | None = Field(default=None, alias="uptimeSeconds")
    load_metrics: LoadMetrics | None = Field(default=None, alias="loadMetrics")
    window_metrics: list[WindowMetrics] | None = Field(default=None, alias="windowMetrics")


class MetricSeriesPoint(BaseModel):
    """时序单点（series/query）。"""

    model_config = {"populate_by_name": True}

    timestamp: str = Field(description="样本时间戳（ISO 8601）")
    value: float


class MetricsSeries(BaseModel):
    """时序查询单条 series（series/query 条目）。"""

    model_config = {"populate_by_name": True}

    metric: str = Field(description="公共业务名（C-METRIC-QUERY-6 回显）")
    labels: dict[str, str]
    window: str | None = None
    points: list[MetricSeriesPoint]
    step_ms: int = Field(alias="stepMs", description="实际生效步长（C-METRIC-RETENTION-2）")


class MetricsRankingItem(BaseModel):
    """排行榜单条记录（rankings/query 条目）。"""

    model_config = {"populate_by_name": True}

    aic: str
    metric: str
    window: str | None = None
    quantile: str | None = None
    value: float
    evaluated_at: str = Field(alias="evaluatedAt", description="评估时刻（ISO 8601）")
    sampled_at: str | None = Field(default=None, alias="sampledAt", description="样本时刻（ISO 8601）")


class MetricsSLOEvaluation(BaseModel):
    """SLO 评估单条结果（slo/evaluate 条目）。"""

    model_config = {"populate_by_name": True}

    aic: str
    window: str
    meets: bool
    target: float
    actual: float
    sli: Literal["success_rate", "p95_latency_ms", "p99_latency_ms", "avg_latency_ms"]
    observed_at: str = Field(alias="observedAt", description="观测时刻（ISO 8601）")


class MetricsCapacitySaturationItem(BaseModel):
    """容量饱和度单条记录（capacity/saturation 条目）。"""

    model_config = {"populate_by_name": True}

    aic: str
    active_ratio: float | None = Field(default=None, alias="activeRatio")
    queue_ratio: float | None = Field(default=None, alias="queueRatio")
    active_tasks: int | None = Field(default=None, alias="activeTasks")
    max_active_tasks: int | None = Field(default=None, alias="maxActiveTasks")
    queued_tasks: int | None = Field(default=None, alias="queuedTasks")
    max_queued_tasks: int | None = Field(default=None, alias="maxQueuedTasks")
    sampled_at: str = Field(alias="sampledAt", description="样本时刻（ISO 8601）")


# ── 请求模型（spec §6.3.1） ────────────────────────────────────────────────────


class MetricsSnapshotQueryRequest(BaseModel):
    """snapshots/query 请求体。"""

    model_config = {"populate_by_name": True}

    filter: AMPFilter | None = None
    sort: list[AMPSortSpec] | None = None
    page: AMPPaginationRequest | None = None
    windows: list[str] | None = None
    time_range: AMPTimeRange | None = Field(
        default=None,
        alias="timeRange",
        description="静默忽略（§6.1 第 1 条：snapshots 不支持时间范围）",
    )


class MetricsSeriesQueryRequest(BaseModel):
    """series/query 请求体。"""

    model_config = {"populate_by_name": True}

    metric: str
    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    step: str | None = None
    aggregation: Literal["avg", "min", "max", "sum", "p50", "p75", "p80", "p90", "p95", "p99", "latest"] | None = None
    group_by_aic: bool | None = Field(default=None, alias="groupByAic")
    group_by_labels: list[str] | None = Field(default=None, alias="groupByLabels")


class MetricsRankingQueryRequest(BaseModel):
    """rankings/query 请求体。"""

    model_config = {"populate_by_name": True}

    metric: str
    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    window: str | None = None
    aggregation: Literal["avg", "max", "min", "p95", "p99", "latest"] | None = None
    top_n: int | None = Field(default=None, alias="topN")
    direction: Literal["asc", "desc"] | None = None


class MetricsSLORule(BaseModel):
    """SLO 规则定义。"""

    model_config = {"populate_by_name": True}

    sli: Literal["success_rate", "p95_latency_ms", "p99_latency_ms", "avg_latency_ms"]
    window: str
    target: float


class MetricsSLOEvaluateRequest(BaseModel):
    """slo/evaluate 请求体。"""

    model_config = {"populate_by_name": True}

    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    rules: list[MetricsSLORule]
    include_failed_details: bool | None = Field(default=None, alias="includeFailedDetails")


class MetricsCapacityRequest(BaseModel):
    """capacity/saturation 请求体。"""

    model_config = {"populate_by_name": True}

    active_ratio_threshold: float | None = Field(default=None, alias="activeRatioThreshold")
    queue_ratio_threshold: float | None = Field(default=None, alias="queueRatioThreshold")
    lookback: str | None = None
    filter: AMPFilter | None = None


# ── SLO 专属响应信封（spec §6.3.1，非 AMPQueryResponse 形态） ────────────────────


class MetricsSLOSummary(BaseModel):
    """SLO 评估汇总。"""

    model_config = {"populate_by_name": True}

    total: int
    meets_count: int = Field(alias="meetsCount")
    breach_count: int = Field(alias="breachCount")


class MetricsSLOEvaluateResponse(BaseModel):
    """slo/evaluate 响应（非标准 AMPQueryResponse 信封，有独立 summary）。"""

    model_config = {"populate_by_name": True}

    items: list[MetricsSLOEvaluation]
    summary: MetricsSLOSummary
    meta: AMPResponseMeta
