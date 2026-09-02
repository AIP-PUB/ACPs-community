"""
Leader Agent Platform - LLM-3 反问合并器 (ClarificationMerger)

本模块实现 2.2.5 反问闭环：
- 输入：各 Partner 的 Task.status.dataItems（缺口/字段/格式）+ userContext
- 输出：合并问询文本（text）+ 可选缺口清单（data）

触发条件：观察到任一 Partner AwaitingInput
"""

import asyncio
import json
import logging
import re
from typing import Any

from ..llm.client import LLMClient
from ..models.base import AgentAic
from ..models.clarification import (
    ClarificationMergeInput,
    MergedClarification,
    PartnerClarificationItem,
    RequiredField,
)
from ..services.scenario_loader import ScenarioLoader

logger = logging.getLogger(__name__)


class ClarificationMerger:
 """
    LLM-3 反问合并器。

    将多个 Partner 的 AwaitingInput 需求合并为一次统一的用户问询。

    核心职责：
    1. 收集所有处于 AwaitingInput 状态的 Partner 的缺口信息
    2. 去重与约束合并：同一语义字段只问一次
    3. 调用 LLM 生成自然语言问询
    4. 维护字段到 Partner 的映射（供 LLM-4 分发使用）
 """

    def __init__(
        self,
        llm_client: LLMClient,
        scenario_loader: ScenarioLoader,
    ):
 """
        初始化反问合并器。

        Args:
            llm_client: LLM 客户端
            scenario_loader: 场景加载器
 """
        self._llm_client = llm_client
        self._scenario_loader = scenario_loader

    async def merge(
        self,
        merge_input: ClarificationMergeInput,
    ) -> MergedClarification:
 """
        合并多个 Partner 的澄清需求。

        Args:
            merge_input: 合并请求，包含 Partner 澄清需求列表和上下文信息

        Returns:
            MergedClarification
 """
        partner_items = merge_input.partner_items
        user_query = merge_input.user_query
        user_context = merge_input.user_context
        scenario_id = merge_input.scenario_id

        if not partner_items:
            return MergedClarification(
                question_text="请问有什么可以帮助您的？",
                merged_fields=[],
                source_partners=[],
                field_to_partners={},
            )

 # Step 1: 收集所有字段并建立映射
        merged_fields, field_to_partners = self._merge_fields(partner_items)

 # Step 2: 尝试用 userContext 自动补全
        if user_context:
            merged_fields, field_to_partners = self._auto_fill_from_context(
                merged_fields, field_to_partners, user_context
            )

 # 如果所有字段都被自动补全了，返回空问询
        if not merged_fields:
            return MergedClarification(
                question_text="",  # 空字符串表示不需要反问
                merged_fields=[],
                source_partners=[p.partner_aic for p in partner_items],
                field_to_partners={},
            )

 # Step 3: 调用 LLM 生成问询文本
        question_text = await self._generate_question_text(
            partner_items=partner_items,
            merged_fields=merged_fields,
            user_query=user_query,
            scenario_id=scenario_id,
        )

        return MergedClarification(
            question_text=question_text,
            merged_fields=merged_fields,
            source_partners=[p.partner_aic for p in partner_items],
            field_to_partners=field_to_partners,
        )

    def _merge_fields(
        self,
        partner_items: list[PartnerClarificationItem],
    ) -> tuple[list[RequiredField], dict[str, list[AgentAic]]]:
 """
        合并去重所有 Partner 的字段需求。

        合并规则：
        1. 同名字段只保留一个，但记录所有需要该字段的 Partner
        2. 约束取交集（更严格的约束）
        3. 保持字段标签的一致性

        Args:
            partner_items: Partner 澄清需求列表

        Returns:
            (merged_fields, field_to_partners)
 """
        field_map: dict[str, RequiredField] = {}
        field_to_partners: dict[str, list[AgentAic]] = {}

        for item in partner_items:
            partner_aic = item.partner_aic

            for field in item.required_fields:
                field_name = field.field_name

                if field_name not in field_map:
 # 首次见到该字段
                    field_map[field_name] = field.model_copy
                    field_to_partners[field_name] = [partner_aic]
                else:
 # 已存在，合并约束
                    existing = field_map[field_name]
                    merged = self._merge_field_constraints(existing, field)
                    field_map[field_name] = merged
                    if partner_aic not in field_to_partners[field_name]:
                        field_to_partners[field_name].append(partner_aic)

 # 如果 Partner 只提供了文本问题，没有结构化字段
 # 生成一个通用的 "answer" 字段
            if not item.required_fields and item.question_text:
                generic_field_name = f"answer_{item.dimension_id}"
                if generic_field_name not in field_map:
                    field_map[generic_field_name] = RequiredField(
                        field_name=generic_field_name,
                        field_label=f"{item.dimension_id} 相关信息",
                        field_type="string",
                        description=item.question_text,
                        required=True,
                    )
                    field_to_partners[generic_field_name] = [partner_aic]
                else:
                    if partner_aic not in field_to_partners[generic_field_name]:
                        field_to_partners[generic_field_name].append(partner_aic)

        return list(field_map.values), field_to_partners

    def _merge_field_constraints(
        self,
        field_a: RequiredField,
        field_b: RequiredField,
    ) -> RequiredField:
 """
        合并两个字段的约束。

        取更严格的约束：
        - required: 有一个 True 就是 True
        - constraints: 数值取交集
 """
        merged = field_a.model_copy

 # required 取 OR
        merged.required = field_a.required or field_b.required

 # 合并 constraints
        if field_a.constraints and field_b.constraints:
            merged_constraints = dict(field_a.constraints)
            for key, value in field_b.constraints.items:
                if key in merged_constraints:
 # 数值约束取更严格的
                    if key in ("min", "minValue"):
                        merged_constraints[key] = max(merged_constraints[key], value)
                    elif key in ("max", "maxValue"):
                        merged_constraints[key] = min(merged_constraints[key], value)
 # 枚举取交集
                    elif key in ("enum", "enum_values", "options"):
                        existing_set = set(merged_constraints[key])
                        new_set = set(value)
                        merged_constraints[key] = list(existing_set & new_set)
                else:
                    merged_constraints[key] = value
            merged.constraints = merged_constraints
        elif field_b.constraints:
            merged.constraints = field_b.constraints

 # 保留更详细的描述
        if field_b.description and (not field_a.description or len(field_b.description) > len(field_a.description)):
            merged.description = field_b.description

        return merged

    def _auto_fill_from_context(
        self,
        fields: list[RequiredField],
        field_to_partners: dict[str, list[AgentAic]],
        user_context: dict[str, Any],
    ) -> tuple[list[RequiredField], dict[str, list[AgentAic]]]:
 """
        尝试用已知上下文自动补全字段。

        Args:
            fields: 待补全的字段列表
            field_to_partners: 字段到 Partner 的映射
            user_context: 用户上下文

        Returns:
            (remaining_fields, updated_field_to_partners)
 """
        remaining_fields = []
        updated_mapping: dict[str, list[AgentAic]] = {}

 # 常见字段名映射
        field_name_aliases = {
            "budget": ["budget", "price", "cost", "预算"],
            "check_in_date": ["check_in_date", "checkInDate", "入住日期", "start_date"],
            "check_out_date": [
                "check_out_date",
                "checkOutDate",
                "离店日期",
                "end_date",
            ],
            "destination": ["destination", "city", "目的地", "城市"],
            "travelers": ["travelers", "people", "guests", "人数", "出行人数"],
        }

        for field in fields:
            field_name = field.field_name
            filled = False

 # 直接匹配
            if field_name in user_context:
                filled = True
                logger.debug(f"Field {field_name} auto-filled from context")
            else:
 # 尝试别名匹配
                for canonical, aliases in field_name_aliases.items:
                    if field_name in aliases or field_name.lower in [a.lower for a in aliases]:
                        for alias in aliases:
                            if alias in user_context:
                                filled = True
                                logger.debug(f"Field {field_name} auto-filled via alias {alias}")
                                break
                    if filled:
                        break

            if not filled:
                remaining_fields.append(field)
                updated_mapping[field_name] = field_to_partners.get(field_name, [])

        return remaining_fields, updated_mapping

    async def _generate_question_text(
        self,
        partner_items: list[PartnerClarificationItem],
        merged_fields: list[RequiredField],
        user_query: str | None,
        scenario_id: str | None,
    ) -> str:
 """
        调用 LLM 生成自然语言问询。

        Args:
            partner_items: Partner 澄清需求列表
            merged_fields: 合并后的字段列表
            user_query: 用户原始查询
            scenario_id: 场景 ID

        Returns:
            问询文本
 """
 # 加载 prompts 配置
        prompts_config = self._scenario_loader.get_merged_prompts(scenario_id)
        clarification_config = prompts_config.get("clarification", {})

        system_prompt = clarification_config.get(
            "system_prompt",
            self._default_system_prompt,
        )
        user_prompt_template = clarification_config.get(
            "user_prompt",
            self._default_user_prompt_template,
        )

 # 构建输入 JSON
        input_data = self._build_llm_input(
            partner_items=partner_items,
            merged_fields=merged_fields,
            user_query=user_query,
        )

        user_prompt = user_prompt_template.format(input_json=json.dumps(input_data, ensure_ascii=False, indent=2))

 # 调用 LLM
        llm_profile = self._scenario_loader.get_llm_profile("clarification", scenario_id)
        logger.debug(f"[LLM-3] Using profile: {llm_profile}")
        try:
            response = await asyncio.to_thread(
                self._llm_client.call,
                profile_name=llm_profile,
                system_prompt=system_prompt,
                user_message=user_prompt,
                temperature=0.5,  # 生成自然语言反问，需要适度流畅
            )

 # 解析响应
            question_text = self._parse_llm_response(response)
            return question_text

        except Exception as e:
            logger.error(f"LLM call failed for clarification: {e}")
 # 降级：直接拼接字段生成问询
            return self._fallback_question_text(merged_fields)

    def _build_llm_input(
        self,
        partner_items: list[PartnerClarificationItem],
        merged_fields: list[RequiredField],
        user_query: str | None,
    ) -> dict[str, Any]:
 """构建 LLM 输入 JSON"""
        return {
            "user_query": user_query or "",
            "partner_questions": [
                {
                    "dimension": item.dimension_id,
                    "partner_name": item.partner_name or item.partner_aic[:12],
                    "question": item.question_text,
                }
                for item in partner_items
                if item.question_text
            ],
            "required_fields": [
                {
                    "name": f.field_name,
                    "label": f.field_label,
                    "type": f.field_type,
                    "description": f.description,
                    "required": f.required,
                    "example": f.example,
                }
                for f in merged_fields
            ],
        }

    def _parse_llm_response(self, response: str) -> str:
 """
        解析 LLM 响应。

        支持多种格式：
        1. 纯文本
        2. JSON { "question_text": "..." }
        3. Markdown 代码块包裹的 JSON
 """
        response = response.strip

 # 尝试提取 JSON
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
        if json_match:
            response = json_match.group(1).strip

 # 尝试解析 JSON
        if response.startswith("{"):
            try:
                data = json.loads(response)
                if isinstance(data, dict):
                    return data.get("question_text", data.get("text", response))
            except json.JSONDecodeError:
                pass

 # 直接返回文本
        return response

    def _fallback_question_text(self, fields: list[RequiredField]) -> str:
 """
        降级：根据字段列表生成简单问询。
 """
        if not fields:
            return "请问还有什么需要补充的吗？"

        if len(fields) == 1:
            field = fields[0]
            if field.description:
                return field.description
            return f"请告诉我您的{field.field_label}是什么？"

        field_labels = [f.field_label for f in fields]
        return f"为了更好地为您服务，请告诉我以下信息：{', '.join(field_labels)}。"

    def _default_system_prompt(self) -> str:
 """默认系统提示词"""
 return """你是一个智能助手，负责将多个服务提供方的信息需求合并为一个简洁、友好的问询。

你的任务是：
1. 理解各个服务方需要的信息
2. 将相同或相似的需求合并
3. 生成一个自然流畅的问询，让用户一次性提供所需信息

要求：
- 问询语气要友好、专业
- 不要暴露系统内部的技术细节（如 Partner、维度等概念）
- 如果有多个需求，按逻辑顺序组织
- 问询要简洁明了，避免冗长"""

    def _default_user_prompt_template(self) -> str:
 """默认用户提示词模板"""
 return """请根据以下信息，生成一个面向用户的问询：

{input_json}

请直接输出问询文本，不需要 JSON 格式。"""


# =============================================================================
# 工厂函数
# =============================================================================


_merger_instance: ClarificationMerger | None = None


def get_clarification_merger -> ClarificationMerger:
 """
    获取反问合并器单例。

    Returns:
        ClarificationMerger 实例
 """
    global _merger_instance
    if _merger_instance is None:
        from ..llm.client import get_llm_client

        llm_client = get_llm_client
        scenario_loader = ScenarioLoader
        _merger_instance = ClarificationMerger(
            llm_client=llm_client,
            scenario_loader=scenario_loader,
        )
    return _merger_instance
