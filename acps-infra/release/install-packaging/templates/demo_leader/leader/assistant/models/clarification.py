"""
Leader Agent Platform - 反问/澄清相关模型

本模块定义 LLM-3 反问合并与问询生成相关的数据模型。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import AgentAic, DimensionId

# =============================================================================
# Partner 澄清需求（从 AwaitingInput 状态提取）
# =============================================================================


class RequiredField(BaseModel):
 """
    Partner 需要用户补充的单个字段定义。

    从 Partner Task.status.dataItems 中提取的结构化字段需求。
 """

    field_name: str = Field(
        ...,
        alias="fieldName",
        description="字段名称（如 'budget', 'check_in_date'）",
    )
    field_label: str = Field(
        ...,
        alias="fieldLabel",
        description="字段显示标签（如 '预算', '入住日期'）",
    )
    field_type: str = Field(
        default="string",
        alias="fieldType",
        description="字段类型（string/number/date/enum 等）",
    )
    description: str | None = Field(
        default=None,
        description="字段描述/提示信息",
    )
    required: bool = Field(
        default=True,
        description="是否必填",
    )
    constraints: dict[str, Any] | None = Field(
        default=None,
        description="字段约束（如 min/max/enum_values 等）",
    )
    example: str | None = Field(
        default=None,
        description="示例值",
    )

    model_config = ConfigDict(populate_by_name=True)


class PartnerClarificationItem(BaseModel):
 """
    单个 Partner 的澄清需求。

    当 Partner 处于 AwaitingInput 状态时，从其 Task.status.dataItems 中
    提取的结构化缺口信息。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC",
    )
    partner_name: str | None = Field(
        default=None,
        alias="partnerName",
        description="Partner 名称（用于生成问询文本）",
    )
    dimension_id: DimensionId = Field(
        ...,
        alias="dimensionId",
        description="Partner 负责的业务维度",
    )
    aip_task_id: str = Field(
        ...,
        alias="aipTaskId",
        description="AIP 任务 ID",
    )
    question_text: str | None = Field(
        default=None,
        alias="questionText",
        description="Partner 提出的问题文本（自然语言）",
    )
    required_fields: list[RequiredField] = Field(
        default_factory=list,
        alias="requiredFields",
        description="需要用户补充的结构化字段列表",
    )
    raw_data_items: list[dict[str, Any]] | None = Field(
        default=None,
        alias="rawDataItems",
        description="原始的 Task.status.dataItems（用于调试）",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# LLM-3 输出：合并后的反问结果
# =============================================================================


class MergedClarification(BaseModel):
 """
    LLM-3 的输出：合并后的反问/澄清结果。

    将多个 Partner 的 AwaitingInput 需求合并为一次统一的用户问询。
 """

 # 合并后的问询文本（面向用户）
    question_text: str = Field(
        default="",
        alias="questionText",
        description="合并后的问询文本（面向用户的自然语言）",
    )

 # 可选：结构化的缺口清单（便于前端渲染表单）
    merged_fields: list[RequiredField] = Field(
        default_factory=list,
        alias="mergedFields",
        description="合并去重后的字段清单",
    )

 # 来源追溯：哪些 Partner 贡献了需求
    source_partners: list[AgentAic] = Field(
        default_factory=list,
        alias="sourcePartners",
        description="贡献需求的 Partner AIC 列表",
    )

 # 字段到 Partner 的映射（用于 LLM-4 分发）
    field_to_partners: dict[str, list[AgentAic]] = Field(
        default_factory=dict,
        alias="fieldToPartners",
        description="字段名 -> 需要该字段的 Partner AIC 列表",
    )

 # 是否通过 userContext 自动补全（无需用户反问）
    auto_filled: bool = Field(
        default=False,
        alias="autoFilled",
        description="是否通过 userContext 自动补全了所有缺口",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# LLM-3 输入：澄清合并请求
# =============================================================================


class ClarificationMergeInput(BaseModel):
 """
    LLM-3 的输入：需要合并的 Partner 澄清需求列表。
 """

    partner_items: list[PartnerClarificationItem] = Field(
        ...,
        alias="partnerItems",
        description="各 Partner 的澄清需求",
    )
    user_query: str | None = Field(
        default=None,
        alias="userQuery",
        description="用户原始查询（用于上下文参考）",
    )
    user_context: dict[str, Any] | None = Field(
        default=None,
        alias="userContext",
        description="已知的用户上下文（可用于自动补全）",
    )
    scenario_id: str | None = Field(
        default=None,
        alias="scenarioId",
        description="当前场景 ID",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 辅助函数：从 AIP Task 状态提取澄清需求
# =============================================================================


def extract_clarification_from_task_status(
    partner_aic: str,
    partner_name: str | None,
    dimension_id: str,
    aip_task_id: str,
    data_items: list[dict[str, Any]],
) -> PartnerClarificationItem:
 """
    从 Partner 的 Task.status.dataItems 中提取澄清需求。

    Args:
        partner_aic: Partner AIC
        partner_name: Partner 名称
        dimension_id: 业务维度 ID
        aip_task_id: AIP 任务 ID
        data_items: Task.status.dataItems

    Returns:
        PartnerClarificationItem
 """
    question_text = None
    required_fields = []

    for item in data_items:
        item_type = item.get("type", "")

 # 提取文本类型的问题
        if item_type == "text":
            text = item.get("text", "")
            if text and not question_text:
                question_text = text

 # 提取结构化的字段需求
        elif item_type == "data":
            data = item.get("data", {})
            metadata = item.get("metadata", {})

 # 检查是否是字段需求格式
            if "requiredFields" in data:
                for field_def in data["requiredFields"]:
                    rf = RequiredField(
                        field_name=field_def.get("name", field_def.get("fieldName", "")),
                        field_label=field_def.get("label", field_def.get("fieldLabel", "")),
                        field_type=field_def.get("type", field_def.get("fieldType", "string")),
                        description=field_def.get("description"),
                        required=field_def.get("required", True),
                        constraints=field_def.get("constraints"),
                        example=field_def.get("example"),
                    )
                    required_fields.append(rf)

 # 也支持单字段格式
            elif "fieldName" in data or "name" in data:
                rf = RequiredField(
                    field_name=data.get("fieldName", data.get("name", "")),
                    field_label=data.get("fieldLabel", data.get("label", "")),
                    field_type=data.get("fieldType", data.get("type", "string")),
                    description=data.get("description"),
                    required=data.get("required", True),
                    constraints=data.get("constraints"),
                    example=data.get("example"),
                )
                required_fields.append(rf)

    return PartnerClarificationItem(
        partner_aic=partner_aic,
        partner_name=partner_name,
        dimension_id=dimension_id,
        aip_task_id=aip_task_id,
        question_text=question_text,
        required_fields=required_fields,
        raw_data_items=data_items,
    )
