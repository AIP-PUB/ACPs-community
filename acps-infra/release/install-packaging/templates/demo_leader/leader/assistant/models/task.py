"""
Leader Agent Platform - Task 相关模型

本模块定义任务相关的数据模型，包括 UserResult、PartnerTask、ActiveTask 和 PlanningResult。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .aip import AipTaskSnapshot, DataItem, TaskCommand, TaskState
from .base import (
    ActiveTaskId,
    ActiveTaskStatus,
    AgentAic,
    AipTaskId,
    DimensionId,
    IsoDateTimeString,
    UserResultType,
)

# =============================================================================
# UserResult（用户可见输出）
# =============================================================================


class UserResult(BaseModel):
 """
    Leader 对用户暴露的"会话级输出"。

    统一承载反问与最终结果（它们本质上都是"要让用户看到并采取动作的信息"）。
 """

    type: UserResultType = Field(..., description="结果类型")
    data_items: list[DataItem] = Field(
        default_factory=list,
        alias="dataItems",
        description="面向用户的负载",
    )
    updated_at: IsoDateTimeString = Field(
        ...,
        alias="updatedAt",
        description="输出更新时间",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# PartnerTask 控制面
# =============================================================================


class PartnerTaskControl(BaseModel):
 """
    Partner 子任务的执行控制面（最小集）。
 """

    in_flight: bool = Field(
        default=False,
        alias="inFlight",
        description="是否存在在途请求",
    )
    last_command: TaskCommand | None = Field(
        default=None,
        alias="lastCommand",
        description="最近一次下发的命令",
    )
    last_command_at: IsoDateTimeString | None = Field(
        default=None,
        alias="lastCommandAt",
        description="最近一次命令时间",
    )
    last_get_at: IsoDateTimeString | None = Field(
        default=None,
        alias="lastGetAt",
        description="最近一次轮询 get 的时间",
    )
    next_poll_at: IsoDateTimeString | None = Field(
        default=None,
        alias="nextPollAt",
        description="建议的下一次轮询时间",
    )
    poll_interval_ms: int | None = Field(
        default=None,
        alias="pollIntervalMs",
        description="轮询间隔（毫秒）",
    )
    timeout_ms: int | None = Field(
        default=None,
        alias="timeoutMs",
        description="单次 RPC 超时（毫秒）",
    )
    retry_count: int = Field(
        default=0,
        alias="retryCount",
        description="重试计数",
    )
    backoff_until: IsoDateTimeString | None = Field(
        default=None,
        alias="backoffUntil",
        description="退避到期时间",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# PartnerTask
# =============================================================================


class PartnerTask(BaseModel):
 """
    一个 activeTask 下，对单个 Partner 的子任务视图（AIP task wrapper）。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="子任务归属的 Partner",
    )
    partner_name: str | None = Field(
        default=None,
        alias="partnerName",
        description="Partner 的显示名称（来自 ACS）",
    )
    aip_task_id: AipTaskId = Field(
        ...,
        alias="aipTaskId",
        description="AIP 任务 ID",
    )
    dimensions: list[str] | None = Field(
        default=None,
        description="该 Partner 被分配的业务维度集合",
    )
    last_snapshot: AipTaskSnapshot | None = Field(
        default=None,
        alias="lastSnapshot",
        description="Leader 缓存的最近一次 Task 快照",
    )
    state: TaskState = Field(..., description="最近一次观察到的状态")
    last_state_changed_at: IsoDateTimeString | None = Field(
        default=None,
        alias="lastStateChangedAt",
        description="Partner 状态最近变化时间",
    )
    control: PartnerTaskControl | None = Field(
        default=None,
        description="执行控制面字段",
    )
    sub_query: str | None = Field(
        default=None,
        alias="subQuery",
        description="Leader 对该 Partner 下发的专项任务说明",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 规划阶段产出
# =============================================================================


class PartnerSelection(BaseModel):
 """
    selectedPartners 中的单个选择项。

    包含 Partner 信息 + Skill 选择 + 执行指令，
    执行阶段可直接据此生成 AIP Message。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="选中的 Partner AIC",
    )
    skill_id: str = Field(
        ...,
        alias="skillId",
        description="选中的 Skill ID（必须是该 Partner ACS 中声明的有效 skill）",
    )
    skill_ids: list[str] | None = Field(
        default=None,
        alias="skillIds",
        description="选中的 Skill ID 列表（用于群组模式）",
    )
    skill_name: str | None = Field(
        default=None,
        alias="skillName",
        description="选中的 Skill 名称（便于可读性）",
    )
    reason: str = Field(
        ...,
        description="选择原因（如'覆盖城区景点/覆盖郊区景点/能力最匹配'）",
    )
    instruction_text: str = Field(
        ...,
        alias="instructionText",
        description="给 Partner 的自然语言任务说明",
    )
    instruction_data: dict[str, Any] | None = Field(
        default=None,
        alias="instructionData",
        description="给 Partner 的结构化约束（可选）",
    )
    acs_data: dict[str, Any] | None = Field(
        default=None,
        alias="acsData",
        description="Partner 的 ACS 数据（用于群组模式提取 RPC URL）",
    )

    model_config = ConfigDict(populate_by_name=True)


class DimensionNote(BaseModel):
 """
    维度备注：记录维度状态及原因。
 """

    status: Literal["active", "inactive", "fallback"] = Field(
        ...,
        description="维度状态：active（已激活）、inactive（用户未提及）或 fallback（无可用 Partner）",
    )
    reason: str = Field(
        ...,
        description="原因说明",
    )


class LLMPlanningOutput(BaseModel):
 """
    LLM-2 规划输出模型（仅包含 LLM 需要决策的业务字段）。

    注意：createdAt、scenarioId 等元数据由系统填充，不要求 LLM 输出。
 """

    selected_partners: dict[DimensionId, list[PartnerSelection]] = Field(
        default_factory=dict,
        alias="selectedPartners",
        description="维度 → Partner 选择 + 执行指令",
    )
    dimension_notes: dict[DimensionId, DimensionNote] | None = Field(
        default=None,
        alias="dimensionNotes",
        description="维度备注（可选）：记录 inactive/fallback 维度的原因",
    )

    model_config = ConfigDict(populate_by_name=True)


class PlanningResult(BaseModel):
 """
    全量规划（LLM-2）产出物。

    设计原则：
    1. Partner 输出不可预设：Partner 的行为由 AIP 协议控制，Leader 不预设其输出格式
    2. 结构紧凑：将维度约束、Partner 选择、执行指令合并为单一结构
    3. 执行阶段可直接使用：selectedPartners 包含生成 AIP Message 所需的全部信息
 """

    created_at: IsoDateTimeString | None = Field(
        default=None,
        alias="createdAt",
        description="产出时间（由系统填充）",
    )
    scenario_id: str | None = Field(
        default=None,
        alias="scenarioId",
        description="本轮规划针对的场景（由系统填充）",
    )
    user_query: str | None = Field(
        default=None,
        alias="userQuery",
        description="用户原始 query（由系统填充）",
    )
    selected_partners: dict[DimensionId, list[PartnerSelection]] = Field(
        default_factory=dict,
        alias="selectedPartners",
        description="维度 → Partner 选择 + 执行指令。active 维度数组非空；inactive/fallback 维度数组为空。",
    )
    dimension_notes: dict[DimensionId, DimensionNote] | None = Field(
        default=None,
        alias="dimensionNotes",
        description="维度备注（可选）：记录 inactive/fallback 维度的原因",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# ActiveTask
# =============================================================================


class ActiveTask(BaseModel):
 """
    用户视角的活跃任务（activeTask）。
 """

    active_task_id: ActiveTaskId = Field(
        ...,
        alias="activeTaskId",
        description="用户视角任务 ID",
    )
    created_at: IsoDateTimeString = Field(
        ...,
        alias="createdAt",
        description="任务创建时间",
    )
    external_status: ActiveTaskStatus = Field(
        ...,
        alias="externalStatus",
        description="对外的收敛状态",
    )
    planning: PlanningResult | None = Field(
        default=None,
        description="规划阶段的结构化产物",
    )
    partner_tasks: dict[AgentAic, PartnerTask] = Field(
        default_factory=dict,
        alias="partnerTasks",
        description="Partner 子任务列表",
    )
    dimension_map: dict[str, AgentAic] | None = Field(
        default=None,
        alias="dimensionMap",
        description="维度到 Partner 的映射",
    )

    model_config = ConfigDict(populate_by_name=True)
