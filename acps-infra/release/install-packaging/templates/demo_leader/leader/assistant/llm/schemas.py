"""
Leader Agent Platform - LLM Schemas

本模块定义所有 LLM 调用点（LLM-1 到 LLM-7）的输入/输出结构。

LLM 调用点概览：
- LLM-1: 意图判定/路由 → IntentDecision
- LLM-2: 全量规划 → PlanningResult
- LLM-3: 反问合并 → ClarificationResult
- LLM-4: 补充输入路由 → TaskInputRoutingResult
- LLM-5: 完成闸门 → CompletionGateResult
- LLM-6: 结果整合 → AggregationResult
- LLM-7: 历史压缩 → HistoryCompressionResult
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import (
    ActiveTaskId,
    AgentAic,
    AipTaskId,
    DialogTurn,
    MissingFieldSpec,
)

# =============================================================================
# LLM-1: 意图判定/路由 (Intent Analysis)
# =============================================================================


class ScenarioBriefInput(BaseModel):
 """场景简要信息输入。"""

    id: str = Field(..., description="场景 ID")
    name: str = Field(..., description="场景名称")
    description: str = Field(..., description="场景描述")
    keywords: list[str] = Field(default_factory=list, description="路由关键词")


class ExpertScenarioInput(BaseModel):
 """当前激活的专业场景输入。"""

    id: str = Field(..., description="场景 ID")
    name: str = Field(..., description="场景名称")
    description: str = Field(..., description="场景描述")


class AwaitingInputGap(BaseModel):
 """Partner 等待输入的缺口字段。"""

    field: str = Field(..., description="字段名")
    description: str = Field(..., description="字段描述")
    required: bool = Field(..., description="是否必填")


class PartnerTaskSummary(BaseModel):
 """Partner 子任务摘要。"""

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC",
    )
    partner_name: str = Field(
        ...,
        alias="partnerName",
        description="Partner 名称",
    )
    state: str = Field(..., description="AIP TaskState")
    awaiting_input_gaps: list[AwaitingInputGap] | None = Field(
        default=None,
        alias="awaitingInputGaps",
        description="缺口字段（仅当 state=awaiting-input 时存在）",
    )

    model_config = ConfigDict(populate_by_name=True)


class ActiveTaskInput(BaseModel):
 """当前活跃任务输入。"""

    active_task_id: ActiveTaskId = Field(
        ...,
        alias="activeTaskId",
        description="活跃任务 ID",
    )
    external_status: str = Field(
        ...,
        alias="externalStatus",
        description="对外状态",
    )
    partner_task_summaries: list[PartnerTaskSummary] = Field(
        default_factory=list,
        alias="partnerTaskSummaries",
        description="Partner 子任务状态",
    )

    model_config = ConfigDict(populate_by_name=True)


class DialogContextInput(BaseModel):
 """对话上下文输入。"""

    recent_turns: list[DialogTurn] = Field(
        default_factory=list,
        alias="recentTurns",
        description="最近 N 轮原始交互",
    )
    history_summary: str | None = Field(
        default=None,
        alias="historySummary",
        description="更早历史的压缩摘要",
    )

    model_config = ConfigDict(populate_by_name=True)


class ClarificationField(BaseModel):
 """反问涉及的字段。"""

    field: str = Field(..., description="字段名")
    description: str = Field(..., description="字段描述")
    required: bool = Field(..., description="是否必填")


class UserResultInput(BaseModel):
 """用户结果状态输入。"""

    type: str | None = Field(default=None, description="结果类型")
    clarification_text: str | None = Field(
        default=None,
        alias="clarificationText",
        description="上一次反问的问题文本",
    )
    clarification_fields: list[ClarificationField] | None = Field(
        default=None,
        alias="clarificationFields",
        description="上一次反问涉及的字段",
    )

    model_config = ConfigDict(populate_by_name=True)


class LLM1InputContext(BaseModel):
 """
    LLM-1（意图判定）输入上下文。
 """

    user_query: str = Field(
        ...,
        alias="userQuery",
        description="用户当前输入",
    )
    scenario: dict[str, Any] = Field(
        ...,
        description="场景信息（scenarioBriefs + expertScenario）",
    )
    task: dict[str, Any] = Field(
        ...,
        description="任务信息（activeTask）",
    )
    session: dict[str, Any] = Field(
        ...,
        description="会话信息（userContext + dialogContext + userResult）",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-1 输出：IntentDecision（已在 models/intent.py 中定义）


# =============================================================================
# LLM-2: 全量规划 (Planning)
# =============================================================================


class LLM2InputContext(BaseModel):
 """
    LLM-2（全量规划）输入上下文。

    输入：userQuery + domain + partners + skills
 """

    user_query: str = Field(
        ...,
        alias="userQuery",
        description="用户输入",
    )
    scenario_id: str = Field(
        ...,
        alias="scenarioId",
        description="当前场景 ID",
    )
    domain_meta: dict[str, Any] = Field(
        ...,
        alias="domainMeta",
        description="领域元数据（维度定义、一致性规则等）",
    )
    available_partners: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="availablePartners",
        description="可用 Partner 列表（含 skills 摘要）",
    )
    user_context: dict[str, Any] = Field(
        default_factory=dict,
        alias="userContext",
        description="用户上下文",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-2 输出：PlanningResult（已在 models/task.py 中定义）


# =============================================================================
# LLM-3: 反问合并 (Clarification)
# =============================================================================


class PartnerAwaitingInputGaps(BaseModel):
 """单个 Partner 的缺口信息。"""

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC",
    )
    partner_name: str = Field(
        ...,
        alias="partnerName",
        description="Partner 名称",
    )
    gaps: list[MissingFieldSpec] = Field(
        default_factory=list,
        description="缺口字段列表",
    )

    model_config = ConfigDict(populate_by_name=True)


class LLM3InputContext(BaseModel):
 """
    LLM-3（反问合并）输入上下文。

    输入：各 Partner 的 awaitingInputGaps
 """

    partners_gaps: list[PartnerAwaitingInputGaps] = Field(
        default_factory=list,
        alias="partnersGaps",
        description="各 Partner 的缺口信息",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-3 输出：ClarificationResult（已在 models/intent.py 中定义）


# =============================================================================
# LLM-4: 补充输入路由 (Input Routing)
# =============================================================================


class LLM4InputContext(BaseModel):
 """
    LLM-4（补充输入路由）输入上下文。

    输入：用户回答 + 各 Partner 的 awaitingInputGaps
 """

    user_query: str = Field(
        ...,
        alias="userQuery",
        description="用户回答",
    )
    partners_gaps: list[PartnerAwaitingInputGaps] = Field(
        default_factory=list,
        alias="partnersGaps",
        description="各 Partner 的缺口信息",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-4 输出：TaskInputRoutingResult（已在 models/intent.py 中定义）


# =============================================================================
# LLM-5: 完成闸门 (Completion Gate)
# =============================================================================


class PartnerOutput(BaseModel):
 """单个 Partner 的产出物。"""

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC",
    )
    aip_task_id: AipTaskId = Field(
        ...,
        alias="aipTaskId",
        description="AIP 任务 ID",
    )
    output_data: dict[str, Any] = Field(
        default_factory=dict,
        alias="outputData",
        description="产出数据",
    )

    model_config = ConfigDict(populate_by_name=True)


class LLM5InputContext(BaseModel):
 """
    LLM-5（完成闸门）输入上下文。

    输入：各 Partner 产出物 + 一致性规则 + 用户约束
 """

    partners_outputs: list[PartnerOutput] = Field(
        default_factory=list,
        alias="partnersOutputs",
        description="各 Partner 产出物",
    )
    consistency_rules: dict[str, Any] | None = Field(
        default=None,
        alias="consistencyRules",
        description="一致性规则",
    )
    user_context: dict[str, Any] = Field(
        default_factory=dict,
        alias="userContext",
        description="用户约束",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-5 输出：CompletionGateResult（已在 models/intent.py 中定义）


# =============================================================================
# LLM-6: 结果整合 (Aggregation)
# =============================================================================


class PartnerFinalOutput(BaseModel):
 """单个 Partner 的最终产出。"""

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC",
    )
    dimension: str | None = Field(
        default=None,
        description="负责的维度",
    )
    output_data: dict[str, Any] = Field(
        default_factory=dict,
        alias="outputData",
        description="产出数据",
    )
    status: str = Field(..., description="完成状态（completed/failed/degraded）")
    failure_reason: str | None = Field(
        default=None,
        alias="failureReason",
        description="失败原因（如果 status=failed）",
    )

    model_config = ConfigDict(populate_by_name=True)


class LLM6InputContext(BaseModel):
 """
    LLM-6（结果整合）输入上下文。

    输入：所有 Partner 产出 + 失败/降级信息 + dialogContext
 """

    partners_outputs: list[PartnerFinalOutput] = Field(
        default_factory=list,
        alias="partnersOutputs",
        description="所有 Partner 产出",
    )
    dialog_context: DialogContextInput | None = Field(
        default=None,
        alias="dialogContext",
        description="对话上下文",
    )
    user_context: dict[str, Any] = Field(
        default_factory=dict,
        alias="userContext",
        description="用户上下文",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-6 输出：AggregationResult（已在 models/intent.py 中定义）


# =============================================================================
# LLM-7: 历史压缩 (History Compression)
# =============================================================================


class LLM7InputContext(BaseModel):
 """
    LLM-7（历史压缩）输入上下文。
 """

    recent_turns: list[DialogTurn] = Field(
        default_factory=list,
        alias="recentTurns",
        description="当前所有轮次（按时间正序）",
    )
    existing_history_summary: str | None = Field(
        default=None,
        alias="existingHistorySummary",
        description="之前已有的历史摘要",
    )
    keep_recent_count: int = Field(
        default=5,
        alias="keepRecentCount",
        description="需要保留的最近轮次数量",
    )

    model_config = ConfigDict(populate_by_name=True)


# LLM-7 输出：HistoryCompressionResult（已在 models/intent.py 中定义）
