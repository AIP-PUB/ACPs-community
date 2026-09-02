"""
Leader Agent Platform - 意图决策相关模型

本模块定义意图分析（LLM-1）和增量更新（LLM-4）相关的数据模型。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import AgentAic, AipTaskId, IntentType

# =============================================================================
# 意图决策（LLM-1 输出）
# =============================================================================


class TaskInstruction(BaseModel):
 """
    任务操作指令。

    TaskNew：概括新任务的执行目标
    TaskInput：概括用户补充了哪些关键信息
 """

    text: str = Field(..., description="自然语言概括")
    data: dict[str, Any] | None = Field(
        default=None,
        description="结构化数据（可选）",
    )


class IntentDecision(BaseModel):
 """
    意图分析输出的决策对象（LLM-1 的结构化产出）。
 """

    intent_type: IntentType = Field(
        ...,
        alias="intentType",
        description="意图类型",
    )
    target_scenario: str | None = Field(
        default=None,
        alias="targetScenario",
        description="目标专业场景 ID（仅在 TaskNew 且需要切换时填写）",
    )
    task_instruction: TaskInstruction | None = Field(
        default=None,
        alias="taskInstruction",
        description="任务操作指令",
    )
    response_guide: str | None = Field(
        default=None,
        alias="responseGuide",
        description="对闲聊/拒绝服务的回复指引",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 反问闭环（LLM-3 相关）
# =============================================================================


class MissingFieldSpec(BaseModel):
 """
    AwaitingInput 时，Leader 希望用户补充的"缺口字段定义"。

    用途：
    - LLM-3 需要把多个 Partner 的缺口合并为一次问询
    - 前端可用该结构渲染表单
 """

    field: str = Field(..., description="字段 key（稳定主键）")
    description: str | None = Field(default=None, description="人类可读描述")
    required: bool = Field(default=True, description="是否必填")
    expected_type: str | None = Field(
        default=None,
        alias="expectedType",
        description="字段类型提示",
    )
    examples: list[Any] | None = Field(
        default=None,
        description="示例值",
    )
    constraints: dict[str, Any] | None = Field(
        default=None,
        description="额外约束",
    )

    model_config = ConfigDict(populate_by_name=True)


class ClarificationResult(BaseModel):
 """
    LLM-3 的输出：合并后的用户问询。
 """

    text: str = Field(..., description="给用户的澄清问题文本")
    missing_fields: list[MissingFieldSpec] = Field(
        default_factory=list,
        alias="missingFields",
        description="缺口字段列表",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 增量更新（LLM-4 相关）
# =============================================================================


class PartnerInputPatch(BaseModel):
 """
    对单个 Partner 的增量补丁。
 """

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
    patch_text: str = Field(
        ...,
        alias="patchText",
        description="给 Partner 的补充说明文本",
    )
    patch_data: dict[str, Any] | None = Field(
        default=None,
        alias="patchData",
        description="结构化补丁",
    )

    model_config = ConfigDict(populate_by_name=True)


class TaskInputRoutingResult(BaseModel):
 """
    LLM-4 的输出：把一次用户回答分解为对多个 Partner 的定向补丁。
 """

    is_sufficient: bool = Field(
        ...,
        alias="isSufficient",
        description="信息是否足够继续推进",
    )
    patches_by_partner: dict[AgentAic, PartnerInputPatch] = Field(
        default_factory=dict,
        alias="patchesByPartner",
        description="按 Partner 分组的补丁列表",
    )
    missing_fields: list[MissingFieldSpec] = Field(
        default_factory=list,
        alias="missingFields",
        description="仍缺失的字段（当 isSufficient=false 时）",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 完成闸门（LLM-5 相关）
# =============================================================================


class CompletionDecision(BaseModel):
 """
    对单个 Partner 的完成/继续决策。
 """

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
    next_action: str = Field(
        ...,
        alias="nextAction",
        description="complete 或 continue",
    )
    followup: TaskInstruction | None = Field(
        default=None,
        description="仅 continue 时填写的后续指令",
    )
    conflicts: list[str] = Field(
        default_factory=list,
        description="检测到的冲突列表",
    )

    model_config = ConfigDict(populate_by_name=True)


class CompletionGateResult(BaseModel):
 """
    LLM-5 的输出：对各 Partner 的 complete/continue 决策。
 """

    decided_at: str = Field(
        ...,
        alias="decidedAt",
        description="决策时间",
    )
    decisions: list[CompletionDecision] = Field(
        default_factory=list,
        description="决策列表",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 结果整合（LLM-6 相关）
# =============================================================================


class AggregationResult(BaseModel):
 """
    LLM-6 的输出：整合后的最终用户回复。
 """

    type: str = Field(default="final", description="结果类型")
    text: str = Field(..., description="Markdown 格式的最终回复")
    structured: dict[str, Any] | None = Field(
        default=None,
        description="结构化数据（可选）",
    )


# =============================================================================
# 历史压缩（LLM-7 相关）
# =============================================================================


class HistoryCompressionResult(BaseModel):
 """
    LLM-7 的输出：压缩后的历史摘要与保留的最近轮次。
 """

    new_history_summary: str = Field(
        ...,
        alias="newHistorySummary",
        description="压缩后的历史摘要（150-300 字）",
    )
    kept_turns: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="keptTurns",
        description="保留的最近轮次（原样返回）",
    )

    model_config = ConfigDict(populate_by_name=True)
