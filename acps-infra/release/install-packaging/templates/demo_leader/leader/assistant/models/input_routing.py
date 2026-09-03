"""
Leader Agent Platform - 增量更新/输入路由相关模型 (LLM-4)

本模块定义 LLM-4 补充输入分解与定向分发相关的数据模型。

触发条件：intent_type = TASK_INPUT（用户回答 AwaitingInput 的补充输入）
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import AgentAic, AipTaskId, DimensionId
from .clarification import MergedClarification, RequiredField

# =============================================================================
# Partner 的 AwaitingInput 缺口信息（LLM-4 的输入组成部分）
# =============================================================================


class PartnerGapInfo(BaseModel):
 """
    单个 Partner 的 AwaitingInput 缺口信息。

    从 Task.status.dataItems 提取的结构化缺口描述。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC 标识",
    )
    partner_name: str | None = Field(
        default=None,
        alias="partnerName",
        description="Partner 名称（便于生成说明文本）",
    )
    dimension_id: DimensionId = Field(
        ...,
        alias="dimensionId",
        description="Partner 负责的业务维度",
    )
    aip_task_id: AipTaskId = Field(
        ...,
        alias="aipTaskId",
        description="AIP 任务 ID",
    )
    awaiting_fields: list[RequiredField] = Field(
        default_factory=list,
        alias="awaitingFields",
        description="该 Partner 等待的字段列表（从 dataItems 提取）",
    )
    question_text: str | None = Field(
        default=None,
        alias="questionText",
        description="Partner 原始的自然语言问题（可选）",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# LLM-4 输入：增量更新请求
# =============================================================================


class InputRoutingRequest(BaseModel):
 """
    LLM-4 的输入：用户补充输入 + 各 Partner 的缺口定义。

    触发条件：LLM-1 判定 intent_type = TASK_INPUT
 """

    user_input: str = Field(
        ...,
        alias="userInput",
        description="用户的补充输入（自然语言）",
    )
    partner_gaps: list[PartnerGapInfo] = Field(
        ...,
        alias="partnerGaps",
        description="各处于 AwaitingInput 状态的 Partner 的缺口信息",
    )
    active_task_id: str = Field(
        ...,
        alias="activeTaskId",
        description="当前活跃任务 ID",
    )
    last_clarification: MergedClarification | None = Field(
        default=None,
        alias="lastClarification",
        description="LLM-3 生成的上一次合并反问（用于对照）",
    )
    user_context: dict[str, Any] | None = Field(
        default=None,
        alias="userContext",
        description="用户上下文（已知信息，可用于补全）",
    )
    scenario_id: str | None = Field(
        default=None,
        alias="scenarioId",
        description="当前场景 ID",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# Partner 补丁：给单个 Partner 的定向更新
# =============================================================================


class PartnerPatch(BaseModel):
 """
    对单个 Partner 的增量补丁。

    用于生成 AIP Message(command=continue) 的内容。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="目标 Partner AIC",
    )
    aip_task_id: AipTaskId = Field(
        ...,
        alias="aipTaskId",
        description="AIP 任务 ID",
    )
    patch_text: str = Field(
        ...,
        alias="patchText",
        description="给 Partner 的补充说明文本（让 Partner 明白用户补充/更改了什么）",
    )
    patch_data: dict[str, Any] | None = Field(
        default=None,
        alias="patchData",
        description="结构化补丁（字段级 patch，减少 Partner 误解）",
    )
    filled_fields: list[str] = Field(
        default_factory=list,
        alias="filledFields",
        description="本次补丁填充的字段列表（用于追踪）",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# LLM-4 输出：输入路由结果
# =============================================================================


class InputRoutingResult(BaseModel):
 """
    LLM-4 的输出：把一次用户回答分解为对多个 Partner 的定向补丁。

    核心目标：确保"一个用户回答 → 多个 Partner 定向 continue"的映射是类型可表达的。
 """

    is_sufficient: bool = Field(
        ...,
        alias="isSufficient",
        description=("信息是否足够继续推进。True: 可以对 Partner 发送 continue；False: 应进入反问闭环（LLM-3）"),
    )
    patches_by_partner: dict[AgentAic, PartnerPatch] = Field(
        default_factory=dict,
        alias="patchesByPartner",
        description="按 Partner 分组的补丁，key 为 Partner AIC",
    )
    missing_fields: list[RequiredField] = Field(
        default_factory=list,
        alias="missingFields",
        description="若 isSufficient=False，这里给出仍缺失的字段清单",
    )
    routing_summary: str | None = Field(
        default=None,
        alias="routingSummary",
        description="路由决策的简要说明（用于调试/日志）",
    )

    model_config = ConfigDict(populate_by_name=True)

    def get_target_partners(self) -> list[AgentAic]:
 """获取需要发送 continue 的 Partner 列表"""
        return list(self.patches_by_partner.keys)

    def has_patch_for(self, partner_aic: AgentAic) -> bool:
 """检查指定 Partner 是否有补丁"""
        return partner_aic in self.patches_by_partner


# =============================================================================
# Continue Message 计划：转换为可发送的 AIP Message
# =============================================================================


class ContinueMessagePlan(BaseModel):
 """
    将结构化补丁转换为可发送的 AIP Message 的计划。

    用于 Executor 执行 continue 操作。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="目标 Partner AIC",
    )
    aip_task_id: AipTaskId = Field(
        ...,
        alias="aipTaskId",
        description="AIP 任务 ID",
    )
    endpoint: str | None = Field(
        default=None,
        description="Partner RPC 端点（可选，可从 ACS 获取）",
    )
    message_text: str = Field(
        ...,
        alias="messageText",
        description="Message.dataItems 中的文本说明",
    )
    message_data: dict[str, Any] | None = Field(
        default=None,
        alias="messageData",
        description="Message.dataItems 中的结构化数据",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 辅助函数
# =============================================================================


def extract_partner_gaps_from_execution_result(
    execution_result,
    planning_result=None,
) -> list[PartnerGapInfo]:
 """
    从执行结果中提取各 Partner 的缺口信息。

    Args:
        execution_result: ExecutionResult 对象
        planning_result: PlanningResult 对象（用于获取 Partner 名称）

    Returns:
        PartnerGapInfo 列表
 """
    gaps = []

    for partner_aic in execution_result.awaiting_input_partners:
        partner_result = execution_result.partner_results.get(partner_aic)
        if not partner_result:
            continue

 # 获取 Partner 名称
        partner_name = None
        if planning_result and hasattr(planning_result, "selected_partners"):
            for dim_id, partners in planning_result.selected_partners.items:
                for p in partners:
                    p_aic = getattr(p, "partner_aic", None) or getattr(p, "aic", None)
                    if p_aic == partner_aic:
                        partner_name = getattr(p, "name", None) or getattr(p, "skill_name", None)
                        break
                if partner_name:
                    break

 # 从 dataItems 提取字段
        awaiting_fields = []
        question_text = None

        if partner_result.task and partner_result.task.status.dataItems:
            for item in partner_result.task.status.dataItems:
                item_dict = item.model_dump if hasattr(item, "model_dump") else item

 # 提取文本问题
                if item_dict.get("type") == "text":
                    text = item_dict.get("text", "")
                    if text and not question_text:
                        question_text = text

 # 提取结构化字段
                elif item_dict.get("type") == "data":
                    data = item_dict.get("data", {})
                    if "requiredFields" in data:
                        for field_def in data["requiredFields"]:
                            rf = RequiredField(
                                field_name=field_def.get("name", field_def.get("fieldName", "")),
                                field_label=field_def.get("label", field_def.get("fieldLabel", "")),
                                field_type=field_def.get("type", "string"),
                                description=field_def.get("description"),
                                required=field_def.get("required", True),
                                constraints=field_def.get("constraints"),
                                example=field_def.get("example"),
                            )
                            awaiting_fields.append(rf)

 # 也从 questions_for_user 提取
        if not awaiting_fields and hasattr(execution_result, "questions_for_user"):
            for q in execution_result.questions_for_user:
                if hasattr(q, "text") and not question_text:
                    question_text = q.text

        gap_info = PartnerGapInfo(
            partner_aic=partner_aic,
            partner_name=partner_name,
            dimension_id=partner_result.dimension_id or "unknown",
            aip_task_id=(partner_result.task.id if partner_result.task else f"task-{partner_aic[:8]}"),
            awaiting_fields=awaiting_fields,
            question_text=question_text,
        )
        gaps.append(gap_info)

    return gaps


def build_continue_message_plans(
    routing_result: InputRoutingResult,
    acs_cache: dict[str, Any] | None = None,
) -> list[ContinueMessagePlan]:
 """
    从路由结果构建 Continue Message 计划列表。

    Args:
        routing_result: InputRoutingResult
        acs_cache: ACS 缓存（用于获取 endpoint）

    Returns:
        ContinueMessagePlan 列表
 """
    plans = []

    for partner_aic, patch in routing_result.patches_by_partner.items:
 # 尝试从 ACS 获取 endpoint
        endpoint = None
        if acs_cache and partner_aic in acs_cache:
            acs_data = acs_cache[partner_aic]
            if isinstance(acs_data, dict):
                endpoints = acs_data.get("endpoints", [])
                if endpoints:
                    endpoint = endpoints[0].get("url") if endpoints else None

        plan = ContinueMessagePlan(
            partner_aic=partner_aic,
            aip_task_id=patch.aip_task_id,
            endpoint=endpoint,
            message_text=patch.patch_text,
            message_data=patch.patch_data,
        )
        plans.append(plan)

    return plans
