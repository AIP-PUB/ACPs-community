"""
Leader Agent Platform - LLM-5 完成闸门 (Completion Gate)

本模块实现 2.3/2.4 交界处的 AwaitingCompletion 决策：
- 输入：各 Partner 的产出物（dataItems/products）+ 场景一致性规则 + 用户硬约束
- 输出：对每个 Partner 的 complete/continue 决策 + conflicts + followupDirectives

触发条件：观察到任一 Partner `AwaitingCompletion` 或进入整合前。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from acps_sdk.aip.aip_base_model import TaskState
from pydantic import BaseModel, ConfigDict, Field

from ..llm.client import LLMClient
from ..models.base import AgentAic, AipTaskId, IsoDateTimeString
from ..services.scenario_loader import ScenarioLoader

logger = logging.getLogger(__name__)


# =============================================================================
# 数据模型
# =============================================================================


class ConflictInfo(BaseModel):
 """冲突/问题信息"""

    code: str = Field(..., description="冲突代码（如 TIME_CONFLICT, BUDGET_EXCEED）")
    message: str = Field(..., description="冲突描述")
    details: dict[str, Any] | None = Field(default=None, description="详细信息")


class FollowupDirective(BaseModel):
 """后续指令（用于 continue 决策）"""

    text: str = Field(..., description="给 Partner 的自然语言指令")
    data: dict[str, Any] | None = Field(default=None, description="结构化数据")


class AwaitingCompletionDecision(BaseModel):
 """LLM-5 对单个 Partner 的决策"""

    partner_aic: AgentAic = Field(..., alias="partnerAic", description="Partner AIC")
    aip_task_id: AipTaskId = Field(..., alias="aipTaskId", description="AIP 任务 ID")
    next_action: str = Field(
        ...,
        alias="nextAction",
        description="下一步动作：complete 或 continue",
    )
    followup: FollowupDirective | None = Field(
        default=None,
        description="若 continue，给出后续指令",
    )
    conflicts: list[ConflictInfo] = Field(
        default_factory=list,
        description="检测到的冲突/问题",
    )

    model_config = ConfigDict(populate_by_name=True)


class AwaitingCompletionGateResult(BaseModel):
 """LLM-5 整体输出"""

    decided_at: IsoDateTimeString = Field(
        ...,
        alias="decidedAt",
        description="决策时间",
    )
    decisions: list[AwaitingCompletionDecision] = Field(
        ...,
        description="对各 Partner 的决策列表",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# Partner 产出物摘要
# =============================================================================


@dataclass
class PartnerProductSummary:
 """Partner 产出物摘要（用于 LLM-5 输入）"""

    partner_aic: str
    aip_task_id: str
    dimension_id: str
    state: str
    data_items: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)


# =============================================================================
# 完成闸门
# =============================================================================


class CompletionGate:
 """
    LLM-5 完成闸门

    负责判断 AwaitingCompletion 状态的 Partner 产出是否满足约束，
    并决定执行 complete 还是 continue。
 """

    def __init__(
        self,
        llm_client: LLMClient,
        scenario_loader: ScenarioLoader,
    ):
 """
        初始化完成闸门。

        Args:
            llm_client: LLM 客户端
            scenario_loader: 场景加载器（用于获取一致性规则）
 """
        self._llm_client = llm_client
        self._scenario_loader = scenario_loader

    async def evaluate(
        self,
        partner_summaries: list[PartnerProductSummary],
        user_constraints: dict[str, Any] | None = None,
        scenario_id: str | None = None,
    ) -> AwaitingCompletionGateResult:
 """
        评估 AwaitingCompletion 状态的 Partner 产出。

        Args:
            partner_summaries: Partner 产出物摘要列表
            user_constraints: 用户硬约束（如预算、时间等）
            scenario_id: 场景 ID（用于获取一致性规则）

        Returns:
            AwaitingCompletionGateResult
 """
 # 过滤出 AwaitingCompletion 状态的 Partner
        awaiting_partners = [ps for ps in partner_summaries if ps.state == TaskState.AwaitingCompletion.value]

        if not awaiting_partners:
 # 没有需要决策的 Partner，返回空结果
            logger.debug("[LLM-5] No partners in AwaitingCompletion state, skipping")
            return AwaitingCompletionGateResult(
                decided_at=datetime.now(UTC).isoformat,
                decisions=[],
            )

        logger.debug(f"[LLM-5] {len(awaiting_partners)} partner(s) awaiting completion decision")

 # 加载场景配置（获取一致性规则）
        consistency_rules = self._load_consistency_rules(scenario_id)

 # 构建 LLM 输入
        input_json = self._build_input_json(
            awaiting_partners=awaiting_partners,
            user_constraints=user_constraints,
            consistency_rules=consistency_rules,
        )

 # 加载 prompt 模板
        prompt_config = self._load_prompt_config(scenario_id)
        system_prompt = prompt_config.get("system", "")
        system_prompt = system_prompt.replace("{{input_json}}", json.dumps(input_json, ensure_ascii=False, indent=2))

 # 调用 LLM
        llm_profile = prompt_config.get("llm_profile", "llm.default")

        import time

        start_time = time.time
        logger.debug(f"[LLM-5] >>> Starting LLM call for completion gate, profile={llm_profile}")

 # 详细日志：输出 LLM 输入摘要
        def _truncate_for_log(text, max_len=200):
            if not text:
                return "<empty>"
            text = str(text)
            return text[:max_len] + "..." if len(text) > max_len else text

        input_summary = f"[LLM-5] Input summary: {len(awaiting_partners)} partner(s)"
        for ps in awaiting_partners:
            input_summary += f"\n  Partner {ps.partner_aic[-8:]}: state={ps.state}"
            if ps.products:
                for i, p in enumerate(ps.products):
                    for j, di in enumerate(p.get("dataItems", []) if isinstance(p, dict) else []):
                        text_val = di.get("text", str(di)) if isinstance(di, dict) else str(di)
                        input_summary += f"\n    products[{i}].item[{j}]: {_truncate_for_log(text_val, 150)}"
            if ps.data_items:
                for i, di in enumerate(ps.data_items):
                    text_val = di.get("text", str(di)) if isinstance(di, dict) else str(di)
                    input_summary += f"\n    dataItems[{i}]: {_truncate_for_log(text_val, 100)}"
        logger.info(input_summary)

        llm_response = await asyncio.to_thread(
            self._llm_client.call,
            profile_name=llm_profile,
            system_prompt=system_prompt,
            user_message="请分析上述输入并给出决策。",
            temperature=0.2,  # 决策判断，需要稳定一致的输出
        )

        elapsed_ms = (time.time - start_time) * 1000
        logger.debug(f"[LLM-5] <<< LLM call completed in {elapsed_ms:.0f}ms")

 # 详细日志：输出 LLM 响应内容
        logger.info(f"[LLM-5] Raw response: {_truncate_for_log(llm_response, 500)}")

 # 解析 LLM 输出
        result = self._parse_llm_response(llm_response, awaiting_partners)

 # 统计决策结果
        complete_count = sum(1 for d in result.decisions if d.next_action == "complete")
        continue_count = sum(1 for d in result.decisions if d.next_action == "continue")
        logger.info(f"[LLM-5] Result: complete={complete_count}, continue={continue_count}, elapsed={elapsed_ms:.0f}ms")

        return result

    def _load_consistency_rules(self, scenario_id: str | None) -> dict[str, Any]:
 """加载场景的一致性规则"""
        if not scenario_id:
            return {}

        try:
            scenario = self._scenario_loader.get_expert_scenario(scenario_id)
            if scenario and hasattr(scenario, "domain") and scenario.domain:
                domain = scenario.domain
                if hasattr(domain, "consistency_rules"):
                    return domain.consistency_rules or {}
        except Exception as e:
            logger.warning(f"Failed to load consistency rules for {scenario_id}: {e}")

        return {}

    def _load_prompt_config(self, scenario_id: str | None) -> dict[str, Any]:
 """加载 prompt 配置"""
 # 尝试从合并的 prompts 加载（expert 覆盖 base）
        try:
            merged_prompts = self._scenario_loader.get_merged_prompts(scenario_id)

 # 检查是否有 completion_gate 配置
 # prompts.toml 格式：[completion_gate] system = "...", llm_profile = "..."
            prompt_config = {}

 # 获取 completion_gate.system
            system_key = "completion_gate.system"
            if system_key in merged_prompts:
                prompt_config["system"] = merged_prompts[system_key]

 # 获取 completion_gate.llm_profile
            profile_key = "completion_gate.llm_profile"
            if profile_key in merged_prompts:
                prompt_config["llm_profile"] = merged_prompts[profile_key]

            if prompt_config:
                return prompt_config

        except Exception as e:
            logger.warning(f"Failed to load prompts for scenario {scenario_id}: {e}")

 # 返回默认配置
        return self._default_prompt_config

    def _default_prompt_config(self) -> dict[str, Any]:
 """
        紧急兜底配置。

        正常情况下应从 base/prompts.toml 加载，此处仅在配置文件
        完全不可用时提供最小化兜底，避免系统崩溃。
 """
        logger.warning("Using emergency fallback prompt config - check prompts.toml")
        return {
            "llm_profile": "llm.fast",
            "system": '输出 JSON: {"decidedAt":"...","decisions":[{"partnerAic":"...","aipTaskId":"...","nextAction":"complete","conflicts":[]}]}\n输入: {{input_json}}',
        }

    def _build_input_json(
        self,
        awaiting_partners: list[PartnerProductSummary],
        user_constraints: dict[str, Any] | None,
        consistency_rules: dict[str, Any],
    ) -> dict[str, Any]:
 """构建 LLM 输入 JSON"""
        return {
            "partnerProducts": [
                {
                    "partnerAic": ps.partner_aic,
                    "aipTaskId": ps.aip_task_id,
                    "dimensionId": ps.dimension_id,
                    "state": ps.state,
                    "dataItems": ps.data_items,
                    "products": ps.products,
                }
                for ps in awaiting_partners
            ],
            "userConstraints": user_constraints or {},
            "consistencyRules": consistency_rules,
        }

    def _parse_llm_response(
        self,
        llm_response: str,
        awaiting_partners: list[PartnerProductSummary],
    ) -> AwaitingCompletionGateResult:
 """解析 LLM 响应"""
        try:
 # 清理响应（移除 markdown 代码块标记和额外文本）
            cleaned = llm_response.strip
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip

 # 如果响应不以 { 开头，尝试找到第一个 {
            if not cleaned.startswith("{"):
                start_idx = cleaned.find("{")
                if start_idx >= 0:
                    cleaned = cleaned[start_idx:]

 # 使用括号计数器找到完整的 JSON 对象
            if cleaned.startswith("{"):
                brace_count = 0
                end_pos = 0
                for i, c in enumerate(cleaned):
                    if c == "{":
                        brace_count += 1
                    elif c == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            end_pos = i + 1
                            break
                if end_pos > 0:
                    cleaned = cleaned[:end_pos]

 # 解析 JSON
            data = json.loads(cleaned)

 # 构建结果
            decisions = []
            for d in data.get("decisions", []):
                raw_conflicts = d.get("conflicts", [])
                decision = AwaitingCompletionDecision(
                    partner_aic=d.get("partnerAic", ""),
                    aip_task_id=d.get("aipTaskId", ""),
                    next_action=d.get("nextAction", "complete"),
                    followup=(
                        FollowupDirective(
                            text=d["followup"].get("text", ""),
                            data=d["followup"].get("data"),
                        )
                        if d.get("followup")
                        else None
                    ),
                    conflicts=self._parse_conflicts(raw_conflicts),
                )
                decisions.append(decision)

            return AwaitingCompletionGateResult(
                decided_at=data.get("decidedAt", datetime.now(UTC).isoformat),
                decisions=decisions,
            )

        except Exception as e:
            logger.error(f"Failed to parse LLM response: {e}")
            logger.debug(f"Raw response: {llm_response}")

 # 降级处理：对所有 AwaitingCompletion 的 Partner 默认 complete
            return self._fallback_result(awaiting_partners)

    def _fallback_result(
        self,
        awaiting_partners: list[PartnerProductSummary],
    ) -> AwaitingCompletionGateResult:
 """降级结果：默认对所有 Partner 执行 complete"""
        return AwaitingCompletionGateResult(
            decided_at=datetime.now(UTC).isoformat,
            decisions=[
                AwaitingCompletionDecision(
                    partner_aic=ps.partner_aic,
                    aip_task_id=ps.aip_task_id,
                    next_action="complete",
                    conflicts=[
                        ConflictInfo(
                            code="FALLBACK",
                            message="LLM 解析失败，降级为默认 complete",
                        )
                    ],
                )
                for ps in awaiting_partners
            ],
        )

    def _parse_conflicts(self, raw_conflicts: Any) -> list[ConflictInfo]:
 """兼容字符串或对象数组形式的 conflicts 字段。"""
        if not isinstance(raw_conflicts, list):
            return []

        parsed_conflicts: list[ConflictInfo] = []
        for conflict in raw_conflicts:
            if isinstance(conflict, str):
                parsed_conflicts.append(ConflictInfo(code="UNSPECIFIED", message=conflict))
                continue

            if isinstance(conflict, dict):
                parsed_conflicts.append(
                    ConflictInfo(
                        code=conflict.get("code", "UNKNOWN"),
                        message=conflict.get("message", ""),
                        details=conflict.get("details"),
                    )
                )

        return parsed_conflicts


# =============================================================================
# 工厂函数
# =============================================================================


def get_completion_gate(
    llm_client: LLMClient | None = None,
    scenario_loader: ScenarioLoader | None = None,
) -> CompletionGate:
 """获取完成闸门实例"""
    if llm_client is None:
        from ..llm.client import get_llm_client

        llm_client = get_llm_client

    if scenario_loader is None:
        from ..services.scenario_loader import ScenarioLoader

        scenario_loader = ScenarioLoader

    return CompletionGate(
        llm_client=llm_client,
        scenario_loader=scenario_loader,
    )
