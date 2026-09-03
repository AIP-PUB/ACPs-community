"""Heartbeat 模块 Pydantic 请求/响应/视图模型（§6.6）。

通用类型从 app.core.amp_api_schema 导入；Sync 线缆模型从 acps_sdk.amp.heartbeat_sync 导入，
均不重复定义、不 re-export。

约定：所有外部字段 camelCase alias、populate_by_name=True、时间字段 str（ISO）、
数值用 int（禁 float；silenceDurationSeconds 取整数秒，向下取整）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.amp_api_schema import AMPFilter, AMPPaginationRequest, AMPResponseMeta, AMPSortSpec, AMPTimeRange


class HeartbeatResponseMetaExt(AMPResponseMeta):
    """Heartbeat 查询响应元信息扩展（spec §6.2.1）。"""

    evaluated_at: str = Field(alias="evaluatedAt", description="此次查询评估时间（ISO 8601 UTC）")
    silence_threshold_seconds: int = Field(alias="silenceThresholdSeconds")
    evict_after_seconds: int = Field(alias="evictAfterSeconds")


class HeartbeatLivenessView(BaseModel):
    """单个 AIC 的 liveness 视图（spec §6.2.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    aic: str
    is_alive: bool = Field(alias="isAlive")
    liveness_state: Literal["alive", "silent"] = Field(alias="livenessState")
    last_seen_at: str = Field(alias="lastSeenAt", description="ISO 8601 UTC")
    source_timestamp: str | None = Field(default=None, alias="sourceTimestamp")
    silence_duration_seconds: int = Field(alias="silenceDurationSeconds", description="向下取整，>=0")


class HeartbeatSilenceRankItem(BaseModel):
    """silence/top 排行条目。"""

    model_config = ConfigDict(populate_by_name=True)

    aic: str
    last_seen_at: str = Field(alias="lastSeenAt")
    silence_duration_seconds: int = Field(alias="silenceDurationSeconds")


class HeartbeatSilenceTopRequest(BaseModel):
    """silence/top 请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    top_n: int | None = Field(default=None, alias="topN")
    min_silence_seconds: int | None = Field(default=None, alias="minSilenceSeconds")
    max_silence_seconds: int | None = Field(default=None, alias="maxSilenceSeconds")
    only_silent: bool = Field(default=False, alias="onlySilent")


class SilenceBucketView(BaseModel):
    """summary 静默时长分桶。"""

    model_config = ConfigDict(populate_by_name=True)

    le_seconds: int = Field(alias="leSeconds", description="上界（含），单位秒")
    count: int


class HeartbeatSummaryView(BaseModel):
    """summary 汇总视图。"""

    model_config = ConfigDict(populate_by_name=True)

    total_known: int = Field(alias="totalKnown")
    alive_count: int = Field(alias="aliveCount")
    silent_count: int = Field(alias="silentCount")
    silence_buckets: list[SilenceBucketView] | None = Field(default=None, alias="silenceBuckets")
    partial: bool | None = None
    responded_shard_count: int | None = Field(default=None, alias="respondedShardCount")
    total_shard_count: int | None = Field(default=None, alias="totalShardCount")


class HeartbeatLivenessQueryRequest(BaseModel):
    """liveness/query 请求体（spec §6.1.2 AMPQueryRequest 子集）。

    time_range 字段显式声明，planner 会主动拒绝非 None 值并返回 422（修复 P2-11）。
    model_config 使用 extra="ignore" 以通过其他未知字段，但 time_range 字段不会静默丢弃。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    filter: AMPFilter | None = None
    sort: list[AMPSortSpec] | None = None
    page: AMPPaginationRequest | None = None
    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")


# ── 响应信封 ───────────────────────────────────────────────────────────────────


class HeartbeatLivenessEnvelope(BaseModel):
    """GET /liveness/{aic} 响应信封。"""

    model_config = ConfigDict(populate_by_name=True)

    data: HeartbeatLivenessView
    meta: HeartbeatResponseMetaExt


class HeartbeatSummaryEnvelope(BaseModel):
    """GET /summary 响应信封。"""

    model_config = ConfigDict(populate_by_name=True)

    data: HeartbeatSummaryView
    meta: HeartbeatResponseMetaExt


class HeartbeatQueryResponse[T](BaseModel):
    """列表端点响应信封（不复用 AMPQueryResponse[T]：meta 静态类型为 AMPResponseMeta，
    FastAPI 按 response_model 序列化会丢弃 evaluatedAt 等扩展字段，§D-3）。
    """

    model_config = ConfigDict(populate_by_name=True)

    items: list[T]
    meta: HeartbeatResponseMetaExt
