"""
Leader Agent Platform - LLM-6 结果整合器 (Aggregator)

本模块实现 2.4 结果整合阶段：
- 输入：所有 Partner 最终产出 + dialogContext + 失败/降级信息
- 输出：userResult（type=final + dataItems[]）

触发条件：满足交付条件（关键维度完成或可降级）
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from acps_sdk.aip.aip_base_model import TaskState
from pydantic import BaseModel, ConfigDict, Field

from ..llm.client import LLMClient
from ..models.base import AgentAic, DimensionId, IsoDateTimeString
from ..services.scenario_loader import ScenarioLoader

logger = logging.getLogger(__name__)


# =============================================================================
# 数据模型
# =============================================================================


class PartnerOutput(BaseModel):
    """Partner 产出物"""

    partner_aic: AgentAic = Field(..., alias="partnerAic")
    partner_name: str | None = Field(default=None, alias="partnerName")
    dimension_id: DimensionId = Field(..., alias="dimensionId")
    state: str = Field(...)  # TaskState value
    data_items: list[dict[str, Any]] = Field(default_factory=list, alias="dataItems")
    products: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)


class DegradationInfo(BaseModel):
    """降级信息"""

    dimension_id: DimensionId = Field(..., alias="dimensionId")
    reason: str = Field(...)
    suggestion: str | None = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)


class AggregationInput(BaseModel):
    """整合器输入"""

    partner_outputs: list[PartnerOutput] = Field(default_factory=list, alias="partnerOutputs")
    degradations: list[DegradationInfo] = Field(default_factory=list)
    user_query: str = Field(..., alias="userQuery")
    dialog_summary: str | None = Field(default=None, alias="dialogSummary")
    user_constraints: dict[str, Any] | None = Field(default=None, alias="userConstraints")

    model_config = ConfigDict(populate_by_name=True)


class AggregationResult(BaseModel):
    """整合结果"""

    type: str = Field(default="final")
    text: str = Field(...)  # Markdown 格式的最终回复
    structured: dict[str, Any] | None = Field(default=None)  # 结构化数据
    created_at: IsoDateTimeString = Field(..., alias="createdAt")

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 整合器
# =============================================================================


class Aggregator:
    """
    LLM-6 结果整合器。

    将多个 Partner 的产出物收敛为对用户可消费的最终结果。

    核心职责：
    1. 收集所有 Partner 的产出（含成功、失败、降级）
    2. 调用 LLM 进行智能整合
    3. 生成面向用户的最终结果
    """

    def __init__(
        self,
        llm_client: LLMClient,
        scenario_loader: ScenarioLoader,
    ):
        """
        初始化整合器。

        Args:
            llm_client: LLM 客户端
            scenario_loader: 场景加载器
        """
        self._llm_client = llm_client
        self._scenario_loader = scenario_loader

    async def aggregate(
        self,
        partner_outputs: list[PartnerOutput],
        degradations: list[DegradationInfo] | None = None,
        user_query: str = "",
        dialog_summary: str | None = None,
        user_constraints: dict[str, Any] | None = None,
        scenario_id: str | None = None,
    ) -> AggregationResult:
        """
        整合多个 Partner 的产出。

        Args:
            partner_outputs: Partner 产出列表
            degradations: 降级信息列表
            user_query: 用户原始查询
            dialog_summary: 对话摘要
            user_constraints: 用户约束
            scenario_id: 场景 ID

        Returns:
            AggregationResult
        """
        degradations = degradations or []

        # 如果没有任何产出，检查是否有场景可以提供兜底
        # 只有当没有场景时才返回默认空结果
        if not partner_outputs and not degradations:
            if not scenario_id:
                return self._build_empty_result()
            # 有场景时，继续调用 LLM-6 让场景提供兜底响应
            logger.info(f"[LLM-6] No partner outputs, but scenario '{scenario_id}' available for fallback")

        # 构建 LLM 输入
        input_json = self._build_input_json(
            partner_outputs=partner_outputs,
            degradations=degradations,
            user_query=user_query,
            dialog_summary=dialog_summary,
            user_constraints=user_constraints,
        )

        # 加载 prompt 配置
        prompt_config = self._load_prompt_config(scenario_id)
        system_prompt = prompt_config.get("system", "")

        # 支持两种占位符：{{input_json}} 和 {{dimension_results}}
        input_json_str = json.dumps(input_json, ensure_ascii=False, indent=2)
        system_prompt = system_prompt.replace("{{input_json}}", input_json_str)
        system_prompt = system_prompt.replace("{{dimension_results}}", input_json_str)

        # 调用 LLM
        llm_profile = prompt_config.get("llm_profile", "llm.default")

        import os
        import time

        # Acceptance knob: skip LLM-6 and stitch partner outputs (set ACPS_LLM6_FORCE_FALLBACK=1).
        if (os.environ.get("ACPS_LLM6_FORCE_FALLBACK") or "").strip() in {
            "1",
            "true",
            "TRUE",
            "yes",
        }:
            logger.warning("[LLM-6] ACPS_LLM6_FORCE_FALLBACK=1 — using fallback aggregate")
            return self._fallback_aggregate(
                partner_outputs=partner_outputs,
                degradations=degradations,
                user_query=user_query,
            )

        start_time = time.time()
        logger.debug(f"[LLM-6] >>> Starting LLM call for aggregation, profile={llm_profile}")

        # 详细日志：输出 LLM-6 的输入数据
        def _truncate(text, max_len=200):
            if not text:
                return "<empty>"
            text = str(text)
            return text[:max_len] + "..." if len(text) > max_len else text

        input_summary = (
            f"[LLM-6] Input details: {len(partner_outputs)} partner output(s), {len(degradations)} degradation(s)"
        )
        for i, po in enumerate(partner_outputs):
            input_summary += f"\n  Partner {po.partner_aic[-8:] if po.partner_aic else 'N/A'} ({po.dimension_id}):"
            if po.products:
                for j, prod_text in enumerate(po.products):
                    input_summary += f"\n    product[{j}]: {_truncate(prod_text, 150)}"
            else:
                input_summary += "\n    products: None"
        logger.info(input_summary)

        try:
            llm_response = await asyncio.to_thread(
                self._llm_client.call,
                profile_name=llm_profile,
                system_prompt=system_prompt,
                user_message="请整合上述所有 Partner 的结果，生成面向用户的最终回复。",
                temperature=0.7,  # 内容生成，需要自然流畅的表达
            )

            elapsed_ms = (time.time() - start_time) * 1000
            logger.debug(f"[LLM-6] <<< LLM call completed in {elapsed_ms:.0f}ms")

            # 详细日志：输出 LLM 原始响应
            logger.info(f"[LLM-6] Raw response: {_truncate(llm_response, 400)}")

            # 解析 LLM 输出
            result = self._parse_llm_response(llm_response, partner_outputs)

            logger.info(
                f"[LLM-6] Result: text_length={len(result.text)}, "
                f"text_preview={_truncate(result.text, 200)}, elapsed={elapsed_ms:.0f}ms"
            )
            return result

        except Exception as e:
            logger.error(f"LLM call failed in aggregation: {e}")
            # 降级：直接拼接所有产出
            return self._fallback_aggregate(
                partner_outputs=partner_outputs,
                degradations=degradations,
                user_query=user_query,
            )

    def _build_input_json(
        self,
        partner_outputs: list[PartnerOutput],
        degradations: list[DegradationInfo],
        user_query: str,
        dialog_summary: str | None,
        user_constraints: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """构建 LLM 输入 JSON"""
        input_data: dict[str, Any] = {
            "userQuery": user_query,
            "partnerOutputs": [],
            "degradations": [],
        }

        # 标记是否没有可用的 Partner
        if not partner_outputs:
            input_data["noAvailablePartners"] = True

        # 添加对话摘要
        if dialog_summary:
            input_data["dialogSummary"] = dialog_summary

        # 添加用户约束
        if user_constraints:
            input_data["userConstraints"] = user_constraints

        # 添加 Partner 产出
        for po in partner_outputs:
            output_item = {
                "partnerAic": po.partner_aic,
                "dimensionId": po.dimension_id,
                "state": po.state,
                "dataItems": po.data_items,
            }
            if po.partner_name:
                output_item["partnerName"] = po.partner_name
            if po.products:
                output_item["products"] = po.products
            if po.error:
                output_item["error"] = po.error
            input_data["partnerOutputs"].append(output_item)

        # 添加降级信息
        for deg in degradations:
            input_data["degradations"].append(
                {
                    "dimensionId": deg.dimension_id,
                    "reason": deg.reason,
                    "suggestion": deg.suggestion,
                }
            )

        return input_data

    def _load_prompt_config(self, scenario_id: str | None) -> dict[str, Any]:
        """加载 prompt 配置"""
        try:
            merged_prompts = self._scenario_loader.get_merged_prompts(scenario_id)

            prompt_config = {}

            # 获取 aggregation.system
            system_key = "aggregation.system"
            if system_key in merged_prompts:
                prompt_config["system"] = merged_prompts[system_key]

            # 获取 aggregation.llm_profile
            profile_key = "aggregation.llm_profile"
            if profile_key in merged_prompts:
                prompt_config["llm_profile"] = merged_prompts[profile_key]

            if prompt_config:
                return prompt_config

        except Exception as e:
            logger.warning(f"Failed to load prompts for scenario {scenario_id}: {e}")

        # 返回默认配置
        return self._default_prompt_config()

    def _default_prompt_config(self) -> dict[str, Any]:
        """
        紧急兜底配置。

        正常情况下应从 base/prompts.toml 加载，此处仅在配置文件
        完全不可用时提供最小化兜底，避免系统崩溃。
        """
        logger.warning("Using emergency fallback prompt config - check prompts.toml")
        return {
            "llm_profile": "llm.pro",
            "system": "你是结果整合器。将 Partner 产出整合成 Markdown 格式的用户回复。\n输入: {{input_json}}\n\n直接输出 Markdown 文本，不需要 JSON 包装。",
        }

    def _parse_llm_response(
        self,
        llm_response: str,
        partner_outputs: list[PartnerOutput],
    ) -> AggregationResult:
        """
        解析 LLM 响应。

        LLM-6 现在直接输出 Markdown 文本，不再需要 JSON 解析。
        """
        # 清理响应：移除可能的 markdown 代码块包装
        cleaned = llm_response.strip()
        if cleaned.startswith("```markdown"):
            cleaned = cleaned[11:]
        if cleaned.startswith("```md"):
            cleaned = cleaned[5:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        return AggregationResult(
            type="final",
            text=cleaned,
            structured=None,
            created_at=datetime.now(UTC).isoformat(),
        )

    def _build_empty_result(self) -> AggregationResult:
        """构建空结果"""
        return AggregationResult(
            type="final",
            text="暂无可用的结果。请稍后再试或提供更多信息。",
            structured=None,
            created_at=datetime.now(UTC).isoformat(),
        )

    def _fallback_aggregate(
        self,
        partner_outputs: list[PartnerOutput],
        degradations: list[DegradationInfo],
        user_query: str,
    ) -> AggregationResult:
        """
        降级整合：直接拼接所有产出。

        当 LLM 调用失败时使用。
        """
        text_parts = []

        # 标题
        text_parts.append("# 查询结果\n")

        if user_query:
            text_parts.append(f"**您的需求**：{user_query}\n")

        # 各 Partner 结果
        if partner_outputs:
            text_parts.append("\n## 详细结果\n")

            for po in partner_outputs:
                dim_name = self._get_dimension_display_name(po.dimension_id)
                text_parts.append(f"\n### {dim_name}\n")

                if po.state == TaskState.Completed.value:
                    # 成功的结果
                    for item in po.data_items:
                        if isinstance(item, dict) and "text" in item:
                            text_parts.append(f"{item['text']}\n")
                        elif isinstance(item, str):
                            text_parts.append(f"{item}\n")
                elif po.state in (
                    TaskState.Failed.value,
                    TaskState.Rejected.value,
                ):
                    # 失败的结果
                    error_msg = po.error or "未知错误"
                    text_parts.append(f"⚠️ 该维度查询失败：{error_msg}\n")
                else:
                    # 其他状态
                    text_parts.append(f"该维度状态：{po.state}\n")
                    for item in po.data_items:
                        if isinstance(item, dict) and "text" in item:
                            text_parts.append(f"{item['text']}\n")

        # 降级说明
        if degradations:
            text_parts.append("\n## 注意事项\n")
            for deg in degradations:
                dim_name = self._get_dimension_display_name(deg.dimension_id)
                text_parts.append(f"- **{dim_name}**：{deg.reason}")
                if deg.suggestion:
                    text_parts.append(f"（建议：{deg.suggestion}）")
                text_parts.append("\n")

        return AggregationResult(
            type="final",
            text="".join(text_parts),
            structured={
                "partnerCount": len(partner_outputs),
                "degradationCount": len(degradations),
                "fallback": True,
            },
            created_at=datetime.now(UTC).isoformat(),
        )

    def _get_dimension_display_name(self, dimension_id: str) -> str:
        """获取维度的显示名称"""
        dimension_names = {
            "hotel": "酒店住宿",
            "transport": "交通出行",
            "food": "餐饮美食",
            "scenic": "景点游览",
            "urban": "城区行程",
            "rural": "郊区行程",
        }
        return dimension_names.get(dimension_id, dimension_id)


# =============================================================================
# 工厂函数
# =============================================================================


def get_aggregator(
    llm_client: LLMClient | None = None,
    scenario_loader: ScenarioLoader | None = None,
) -> Aggregator:
    """获取整合器实例"""
    if llm_client is None:
        from ..llm.client import get_llm_client

        llm_client = get_llm_client()

    if scenario_loader is None:
        from ..services.scenario_loader import ScenarioLoader

        scenario_loader = ScenarioLoader()

    return Aggregator(
        llm_client=llm_client,
        scenario_loader=scenario_loader,
    )
