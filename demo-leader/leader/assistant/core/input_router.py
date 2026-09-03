"""
Leader Agent Platform - LLM-4 补充输入路由器 (InputRouter)

本模块实现 2.2.4 增量更新流程：
- 输入：用户补充回答 + 各 Partner 的 AwaitingInput 缺口定义
- 输出：按 Partner 分组的补丁 + 是否充分标记 + 仍缺失字段

触发条件：LLM-1 判定 intent_type = TASK_INPUT
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from ..llm.client import LLMClient
from ..models.base import AgentAic
from ..models.clarification import MergedClarification, RequiredField
from ..models.input_routing import (
    InputRoutingRequest,
    InputRoutingResult,
    PartnerGapInfo,
    PartnerPatch,
)
from ..services.scenario_loader import ScenarioLoader

logger = logging.getLogger(__name__)


class InputRouter:
    """
    LLM-4 补充输入路由器。

    将用户对澄清问题的回答分解为对各 Partner 的定向补丁。

    核心职责：
    1. 分析用户输入，识别回答了哪些字段
    2. 将字段值路由到对应的 Partner（一个字段可能对应多个 Partner）
    3. 验证信息完整性：是否足够继续推进
    4. 生成结构化补丁（patchText + patchData）
    """

    def __init__(
        self,
        llm_client: LLMClient,
        scenario_loader: ScenarioLoader,
    ):
        """
        初始化输入路由器。

        Args:
            llm_client: LLM 客户端
            scenario_loader: 场景加载器（用于获取 prompts 配置）
        """
        self._llm_client = llm_client
        self._scenario_loader = scenario_loader

    async def route(
        self,
        request: InputRoutingRequest,
    ) -> InputRoutingResult:
        """
        路由用户补充输入到各 Partner。

        完整流程：
        1. 构建字段到 Partner 的映射
        2. 调用 LLM 分析用户输入、提取字段值
        3. 验证完整性
        4. 生成补丁

        Args:
            request: InputRoutingRequest

        Returns:
            InputRoutingResult
        """
        partner_gaps = request.partner_gaps
        user_input = request.user_input

        if not partner_gaps:
            logger.warning("No partner gaps provided, returning empty result")
            return InputRoutingResult(
                is_sufficient=True,
                patches_by_partner={},
                missing_fields=[],
                routing_summary="没有需要路由的 Partner",
            )

        # Step 1: 构建字段到 Partner 的映射
        field_to_partners, all_fields = self._build_field_mapping(partner_gaps)

        # Step 2: 尝试从 userContext 自动补全
        auto_filled_values = {}
        if request.user_context:
            auto_filled_values = self._auto_fill_from_context(all_fields, request.user_context)

        # Step 3: 调用 LLM 分析用户输入并提取字段值
        extracted_values, llm_analysis = await self._extract_field_values(
            user_input=user_input,
            partner_gaps=partner_gaps,
            all_fields=all_fields,
            last_clarification=request.last_clarification,
            scenario_id=request.scenario_id,
        )

        # 合并自动填充和提取的值
        merged_values = {**auto_filled_values, **extracted_values}

        # Step 4: 验证完整性
        is_sufficient, missing_fields = self._validate_completeness(
            all_fields=all_fields,
            filled_values=merged_values,
        )

        # Step 5: 生成补丁
        patches = self._generate_patches(
            partner_gaps=partner_gaps,
            field_to_partners=field_to_partners,
            filled_values=merged_values,
            user_input=user_input,
        )

        # 构建路由摘要
        filled_count = len(merged_values)
        total_count = len(all_fields)
        routing_summary = f"从用户输入中提取了 {filled_count}/{total_count} 个字段，路由到 {len(patches)} 个 Partner"
        if not is_sufficient:
            routing_summary += f"，仍缺 {len(missing_fields)} 个必填字段"

        return InputRoutingResult(
            is_sufficient=is_sufficient,
            patches_by_partner=patches,
            missing_fields=missing_fields,
            routing_summary=routing_summary,
        )

    def _get_llm_profile(self, scenario_id: str | None) -> str:
        """
        获取 LLM profile 名称。

        Args:
            scenario_id: 场景 ID

        Returns:
            LLM profile 名称
        """
        if scenario_id:
            try:
                prompts = self._scenario_loader.load_scenario_prompts(scenario_id)
                llm4_config = prompts.get("llm_4_input_router", {})
                return llm4_config.get("llm_profile", "llm.default")
            except Exception as exc:
                logger.debug("Failed to load scenario input-router profile; using default: %s", exc)
        return "llm.default"

    def _build_field_mapping(
        self,
        partner_gaps: list[PartnerGapInfo],
    ) -> tuple[dict[str, list[AgentAic]], list[RequiredField]]:
        """
        构建字段到 Partner 的映射。

        同一字段可能被多个 Partner 需要（如 check_in_date 被酒店和交通都需要）。

        Args:
            partner_gaps: Partner 缺口列表

        Returns:
            (field_to_partners, all_fields)
        """
        field_to_partners: dict[str, list[AgentAic]] = {}
        field_map: dict[str, RequiredField] = {}

        for gap in partner_gaps:
            for field in gap.awaiting_fields:
                field_name = field.field_name

                if field_name not in field_map:
                    field_map[field_name] = field
                    field_to_partners[field_name] = [gap.partner_aic]
                else:
                    if gap.partner_aic not in field_to_partners[field_name]:
                        field_to_partners[field_name].append(gap.partner_aic)
                    # 合并约束（取更严格的）
                    field_map[field_name] = self._merge_field_constraints(field_map[field_name], field)

        return field_to_partners, list(field_map.values())

    def _merge_field_constraints(
        self,
        field_a: RequiredField,
        field_b: RequiredField,
    ) -> RequiredField:
        """合并两个字段的约束（复用 ClarificationMerger 的逻辑）"""
        merged = field_a.model_copy()
        merged.required = field_a.required or field_b.required

        if field_a.constraints and field_b.constraints:
            merged_constraints = dict(field_a.constraints)
            for key, value in field_b.constraints.items():
                if key in merged_constraints:
                    if key in ("min", "minValue"):
                        merged_constraints[key] = max(merged_constraints[key], value)
                    elif key in ("max", "maxValue"):
                        merged_constraints[key] = min(merged_constraints[key], value)
                    elif key in ("enum", "enum_values", "options"):
                        existing_set = set(merged_constraints[key])
                        new_set = set(value)
                        merged_constraints[key] = list(existing_set & new_set)
                else:
                    merged_constraints[key] = value
            merged.constraints = merged_constraints
        elif field_b.constraints:
            merged.constraints = field_b.constraints

        return merged

    def _auto_fill_from_context(
        self,
        all_fields: list[RequiredField],
        user_context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        尝试从 userContext 自动补全字段值。

        Args:
            all_fields: 所有需要的字段
            user_context: 用户上下文

        Returns:
            自动填充的 field_name -> value 映射
        """
        filled = {}

        # 常见字段名别名映射
        field_aliases = {
            "budget": ["budget", "price", "cost", "预算", "价格"],
            "check_in_date": ["check_in_date", "checkInDate", "入住日期", "start_date"],
            "check_out_date": [
                "check_out_date",
                "checkOutDate",
                "离店日期",
                "end_date",
            ],
            "departure_city": ["departure_city", "from", "出发地", "出发城市"],
            "destination": ["destination", "to", "目的地", "城市"],
            "travelers": ["travelers", "people", "guests", "人数", "出行人数"],
            "date": ["date", "travel_date", "日期", "出行日期"],
        }

        for field in all_fields:
            field_name = field.field_name

            # 直接匹配
            if field_name in user_context:
                filled[field_name] = user_context[field_name]
                continue

            # 别名匹配
            for canonical, aliases in field_aliases.items():
                if field_name in aliases or field_name.lower() in [a.lower() for a in aliases]:
                    for alias in aliases:
                        if alias in user_context:
                            filled[field_name] = user_context[alias]
                            break
                if field_name in filled:
                    break

        return filled

    async def _extract_field_values(
        self,
        user_input: str,
        partner_gaps: list[PartnerGapInfo],
        all_fields: list[RequiredField],
        last_clarification: MergedClarification | None,
        scenario_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """
        调用 LLM 从用户输入中提取字段值。

        Args:
            user_input: 用户输入文本
            partner_gaps: Partner 缺口列表
            all_fields: 所有需要的字段
            last_clarification: 上一次合并反问（提供上下文）
            scenario_id: 场景 ID

        Returns:
            (extracted_values, llm_analysis)
        """
        # 加载 prompts 配置
        prompts_config = self._scenario_loader.get_merged_prompts(scenario_id)
        routing_config = prompts_config.get("input_routing", {})

        system_prompt = routing_config.get(
            "system",
            self._default_system_prompt(),
        )

        # 构建输入 JSON
        input_data = self._build_llm_input(
            user_input=user_input,
            partner_gaps=partner_gaps,
            all_fields=all_fields,
            last_clarification=last_clarification,
        )

        # 替换占位符
        final_system = system_prompt.replace("{{input_json}}", json.dumps(input_data, ensure_ascii=False, indent=2))

        try:
            # 获取 LLM profile
            llm_profile = self._get_llm_profile(scenario_id)

            response = await asyncio.to_thread(
                self._llm_client.call,
                profile_name=llm_profile,
                system_prompt=final_system,
                user_message="请分析用户输入并按契约输出 JSON。",
                temperature=0.1,  # 结构化提取，需要确定性输出
            )

            # 解析 LLM 响应
            extracted, analysis = self._parse_llm_response(response, all_fields)
            return extracted, analysis

        except Exception as e:
            logger.error(f"LLM extraction failed: {e}")
            # 降级：尝试规则匹配
            return self._fallback_extraction(user_input, all_fields), None

    def _build_llm_input(
        self,
        user_input: str,
        partner_gaps: list[PartnerGapInfo],
        all_fields: list[RequiredField],
        last_clarification: MergedClarification | None,
    ) -> dict[str, Any]:
        """构建 LLM 输入 JSON"""
        return {
            "userInput": user_input,
            "lastQuestion": (last_clarification.question_text if last_clarification else None),
            "partnerGaps": [
                {
                    "partnerAic": gap.partner_aic,
                    "partnerName": gap.partner_name,
                    "dimensionId": gap.dimension_id,
                    "awaitingFields": [
                        {
                            "name": f.field_name,
                            "label": f.field_label,
                            "type": f.field_type,
                            "description": f.description,
                            "required": f.required,
                            "constraints": f.constraints,
                        }
                        for f in gap.awaiting_fields
                    ],
                }
                for gap in partner_gaps
            ],
            "allFields": [
                {
                    "name": f.field_name,
                    "label": f.field_label,
                    "type": f.field_type,
                    "required": f.required,
                }
                for f in all_fields
            ],
        }

    def _parse_llm_response(
        self,
        response: str,
        all_fields: list[RequiredField],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """
        解析 LLM 响应。

        期望格式：
        {
            "extractedValues": { "field_name": "value", ... },
            "analysis": { ... }
        }
        """
        response = response.strip()

        # 尝试提取 JSON
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
        if json_match:
            response = json_match.group(1).strip()

        # 如果响应不是以 { 开头，尝试找到第一个 {
        if not response.startswith("{"):
            start_idx = response.find("{")
            if start_idx != -1:
                response = response[start_idx:]

        # 移除 JSON 中的注释（LLM 有时会添加 // 或 /* */ 注释）
        response = re.sub(r"//.*?(?=\n|$)", "", response)  # 移除 // 注释
        response = re.sub(r"/\*.*?\*/", "", response, flags=re.DOTALL)  # 移除 /* */ 注释

        try:
            data = json.loads(response)

            if isinstance(data, dict):
                # 优先查找 extractedValues
                if "extractedValues" in data:
                    extracted = data.get("extractedValues", {})
                    analysis = data.get("analysis")
                    return extracted, analysis

                # 也支持 patchesByPartner 格式（旧格式兼容）
                if "patchesByPartner" in data:
                    # 从 patches 中提取值
                    extracted = {}
                    for partner_aic, patch in data.get("patchesByPartner", {}).items():
                        patch_data = patch.get("patchData", {})
                        extracted.update(patch_data)
                    return extracted, None

                # 兜底：整个对象作为提取值
                return data, None

        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM response as JSON: {response[:200]}...")

        return {}, None

    def _fallback_extraction(
        self,
        user_input: str,
        all_fields: list[RequiredField],
    ) -> dict[str, Any]:
        """
        降级提取：使用简单规则匹配。

        支持常见模式：
        - 数字提取（预算、人数）
        - 日期提取（YYYY-MM-DD 格式）
        - 是/否提取
        """
        extracted = {}
        user_input_lower = user_input.lower()

        for field in all_fields:
            field_name = field.field_name
            field_type = field.field_type

            # 数字类型
            if field_type == "number":
                # 匹配带单位的数字（如 "500元"、"2人"）
                numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:元|块|人|位|间)?", user_input)
                if numbers:
                    # 根据字段名猜测应该用哪个数字
                    if "budget" in field_name or "预算" in field.field_label:
                        # 预算通常是较大的数字
                        extracted[field_name] = float(max(numbers, key=float))
                    elif "people" in field_name or "travelers" in field_name or "人数" in field.field_label:
                        # 人数通常是小数字
                        extracted[field_name] = int(min(numbers, key=lambda x: float(x)))
                    else:
                        extracted[field_name] = float(numbers[0])

            # 日期类型
            elif field_type == "date":
                # 匹配 YYYY-MM-DD
                date_match = re.search(r"\d{4}-\d{2}-\d{2}", user_input)
                if date_match:
                    extracted[field_name] = date_match.group(0)
                else:
                    # 匹配相对日期（明天、后天）
                    if "明天" in user_input:
                        from datetime import timedelta

                        tomorrow = datetime.now() + timedelta(days=1)
                        extracted[field_name] = tomorrow.strftime("%Y-%m-%d")
                    elif "后天" in user_input:
                        from datetime import timedelta

                        day_after = datetime.now() + timedelta(days=2)
                        extracted[field_name] = day_after.strftime("%Y-%m-%d")

            # 布尔类型
            elif field_type == "boolean":
                if any(w in user_input_lower for w in ["是", "好", "行", "yes", "ok"]):
                    extracted[field_name] = True
                elif any(w in user_input_lower for w in ["否", "不", "no", "不行", "不要"]):
                    extracted[field_name] = False

        return extracted

    def _validate_completeness(
        self,
        all_fields: list[RequiredField],
        filled_values: dict[str, Any],
    ) -> tuple[bool, list[RequiredField]]:
        """
        验证信息完整性。

        检查所有必填字段是否已填充。

        Args:
            all_fields: 所有字段
            filled_values: 已填充的值

        Returns:
            (is_sufficient, missing_fields)
        """
        missing_fields = []

        for field in all_fields:
            if field.required:
                value = filled_values.get(field.field_name)
                if value is None or value == "":
                    missing_fields.append(field)

        is_sufficient = len(missing_fields) == 0
        return is_sufficient, missing_fields

    def _generate_patches(
        self,
        partner_gaps: list[PartnerGapInfo],
        field_to_partners: dict[str, list[AgentAic]],
        filled_values: dict[str, Any],
        user_input: str,
    ) -> dict[AgentAic, PartnerPatch]:
        """
        为每个 Partner 生成补丁。

        Args:
            partner_gaps: Partner 缺口列表
            field_to_partners: 字段到 Partner 的映射
            filled_values: 已填充的值
            user_input: 用户原始输入

        Returns:
            partner_aic -> PartnerPatch 映射
        """
        patches: dict[AgentAic, PartnerPatch] = {}

        for gap in partner_gaps:
            partner_aic = gap.partner_aic

            # 收集该 Partner 相关的填充值
            partner_data = {}
            filled_field_names = []

            for field in gap.awaiting_fields:
                field_name = field.field_name
                if field_name in filled_values:
                    partner_data[field_name] = filled_values[field_name]
                    filled_field_names.append(field.field_label or field_name)

            # 即使没有明确的填充字段，也要为 AwaitingInput 状态的 Partner 创建 patch
            # 因为用户的补充输入可能包含 Partner 需要的信息
            # Partner 的 LLM 会在 analysis 阶段重新提取
            if not partner_data and not gap.awaiting_fields:
                # 当没有明确的 awaiting_fields 时，直接传递用户输入
                logger.debug(
                    f"Partner {partner_aic} has no explicit awaiting_fields, creating patch with raw user input"
                )
                patch = PartnerPatch(
                    partner_aic=partner_aic,
                    aip_task_id=gap.aip_task_id,
                    patch_text=f"用户补充信息：{user_input}",
                    patch_data={},
                    filled_fields=[],
                )
                patches[partner_aic] = patch
                continue

            # 如果有明确的 awaiting_fields 但没有匹配的填充值，跳过
            if not partner_data:
                continue

            # 生成 patch_text
            patch_text = self._generate_patch_text(
                partner_name=gap.partner_name,
                dimension_id=gap.dimension_id,
                filled_fields=filled_field_names,
                user_input=user_input,
            )

            patch = PartnerPatch(
                partner_aic=partner_aic,
                aip_task_id=gap.aip_task_id,
                patch_text=patch_text,
                patch_data=partner_data,
                filled_fields=list(partner_data.keys()),
            )
            patches[partner_aic] = patch

        return patches

    def _generate_patch_text(
        self,
        partner_name: str | None,
        dimension_id: str,
        filled_fields: list[str],
        user_input: str,
    ) -> str:
        """
        生成给 Partner 的补充说明文本。

        目标：让 Partner 明白用户补充/更改了什么。
        """
        if len(filled_fields) == 1:
            return f"用户补充了{filled_fields[0]}信息：{user_input}"
        if len(filled_fields) > 1:
            fields_text = "、".join(filled_fields)
            return f"用户补充了以下信息：{fields_text}。原文：{user_input}"
        return f"用户补充输入：{user_input}"

    def _default_system_prompt(self) -> str:
        """默认系统提示词"""
        return """你是「补充输入路由器」。你的任务是从用户输入中提取字段值，并判断信息是否充分。

# 输入数据

<INPUT_JSON>
{{input_json}}
</INPUT_JSON>

# 分析步骤

1. 仔细阅读 lastQuestion（上一次向用户提的问题）和 userInput（用户回答）
2. 对照 allFields 列表，提取用户回答中包含的字段值
3. 注意用户可能用自然语言表达，需要你理解并转换为结构化值
4. 如果用户没有明确回答某个字段，不要猜测

# 字段值提取规则

- 数字类型：提取数值，如"500元" → 500，"2人" → 2
- 日期类型：转换为 YYYY-MM-DD 格式，如"明天" → 计算实际日期
- 字符串类型：直接提取文本
- 布尔类型："是/好/行" → true，"否/不" → false

# 输出契约

输出 JSON 格式：
{
  "extractedValues": {
    "field_name": "extracted_value",
    ...
  },
  "analysis": {
    "confident": true/false,
    "notes": "分析备注"
  }
}

只输出 JSON，不要其他文本。"""


# =============================================================================
# 工厂函数
# =============================================================================


_router_instance: InputRouter | None = None


def get_input_router(
    scenario_loader: ScenarioLoader | None = None,
) -> InputRouter:
    """
    获取输入路由器单例。

    Args:
        scenario_loader: 场景加载器（可选，如果不提供则使用全局单例）

    Returns:
        InputRouter 实例
    """
    global _router_instance
    if _router_instance is None:
        from ..llm.client import get_llm_client

        llm_client = get_llm_client()
        if scenario_loader is None:
            from ..services.scenario_loader import get_scenario_loader

            scenario_loader = get_scenario_loader()
        _router_instance = InputRouter(
            llm_client=llm_client,
            scenario_loader=scenario_loader,
        )
    return _router_instance


def reset_input_router() -> None:
    """重置输入路由器单例（用于测试）"""
    global _router_instance
    _router_instance = None
