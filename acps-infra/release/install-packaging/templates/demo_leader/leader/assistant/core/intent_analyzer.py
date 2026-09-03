"""
Leader Agent Platform - Intent Analyzer (LLM-1)

本模块实现 LLM-1 意图判定/路由功能，包括：
- 构建 LLM-1 输入上下文
- 调用 LLM 获取 IntentDecision
- 意图决策的后处理和验证
"""

import json
import logging
from typing import Any

from ..llm.client import LLMClient, get_llm_client
from ..llm.schemas import (
    LLM1InputContext,
)
from ..models import (
    ActiveTask,
    IntentDecision,
    IntentType,
    Session,
    TaskInstruction,
)
from ..models.exceptions import LLMCallError, LLMParseError
from ..services.scenario_loader import ScenarioLoader

logger = logging.getLogger(__name__)

# 最近保留的对话轮次数量
RECENT_TURNS_LIMIT = 5


class IntentAnalyzer:
 """
    LLM-1 意图分析器。

    负责判定用户输入的意图类型：
    - TASK_NEW: 新任务请求
    - TASK_INPUT: 补充任务输入
    - CHIT_CHAT: 闲聊/通用问答
 """

    def __init__(
        self,
        scenario_loader: ScenarioLoader,
        llm_client: LLMClient | None = None,
    ):
 """
        初始化意图分析器。

        Args:
            scenario_loader: 场景加载器
            llm_client: LLM 客户端（可选，默认使用单例）
 """
        self._scenario_loader = scenario_loader
        self._llm_client = llm_client or get_llm_client

    async def analyze(
        self,
        user_query: str,
        session: Session,
        active_task: ActiveTask | None = None,
    ) -> IntentDecision:
 """
        分析用户意图。

        Args:
            user_query: 用户输入
            session: 当前会话
            active_task: 当前活跃任务（可选）

        Returns:
            IntentDecision 意图决策结果

        Raises:
            LLMCallError: LLM 调用失败
            LLMParseError: 响应解析失败
 """
 # 1. 构建输入上下文
        input_context = self._build_input_context(
            user_query=user_query,
            session=session,
            active_task=active_task,
        )

 # 2. 获取 prompt
 # 从 expert_scenario 获取场景 ID
        scenario_id = session.expert_scenario.id if session.expert_scenario else None
        system_prompt = self._get_system_prompt(scenario_id)
        llm_profile = self._scenario_loader.get_llm_profile("intent_analysis", scenario_id)

 # 3. 构建用户消息
 # 注意：system_prompt 只包含格式说明（告诉 LLM 如何解析输入），
 # 实际数据只在 user_message 中出现一次，避免重复浪费 token。
        input_json = json.dumps(
            input_context.model_dump(by_alias=True, exclude_none=True),
            ensure_ascii=False,
            indent=2,
        )

        user_message = f"<INPUT_JSON>\n{input_json}\n</INPUT_JSON>"

        logger.debug(f"[LLM-1] Input context size: {len(input_json)} chars")
        logger.debug(f"[LLM-1] Using profile: {llm_profile}")

 # 4. 调用 LLM
        import time

        start_time = time.time
        logger.debug("[LLM-1] >>> Starting LLM call for intent analysis...")

        try:
            result = self._llm_client.call_structured(
                profile_name=llm_profile,
                system_prompt=system_prompt,
                user_message=user_message,
                response_model=IntentDecision,
                temperature=0.1,  # 低温度确保一致性
                parse_retry_count=1,
            )

            elapsed_ms = (time.time - start_time) * 1000
            logger.debug(f"[LLM-1] <<< LLM call completed in {elapsed_ms:.0f}ms")

            logger.info(
                f"[LLM-1] Result: intentType={result.intent_type}, "
                f"targetScenario={result.target_scenario}, elapsed={elapsed_ms:.0f}ms"
            )

 # 5. 后处理验证
            normalized = self._validate_and_normalize(result, session, active_task)
            return self._apply_target_scenario_fallback(
                normalized,
                user_query=user_query,
                session=session,
            )

        except LLMCallError, LLMParseError:
            raise
        except Exception as e:
            logger.error(f"Intent analysis failed: {e}")
            raise LLMCallError(f"Intent analysis failed: {e}")

    def _build_input_context(
        self,
        user_query: str,
        session: Session,
        active_task: ActiveTask | None,
    ) -> LLM1InputContext:
 """构建 LLM-1 输入上下文。"""

 # 构建 scenario 部分
        scenario = self._build_scenario_section(session)

 # 构建 task 部分
        task = self._build_task_section(active_task)

 # 构建 session 部分
        session_section = self._build_session_section(session)

        return LLM1InputContext(
            userQuery=user_query,
            scenario=scenario,
            task=task,
            session=session_section,
        )

    def _build_scenario_section(self, session: Session) -> dict[str, Any]:
 """构建场景信息部分。"""
 # 获取所有专业场景简介
        scenario_briefs = [
            {
                "id": brief.id,
                "name": brief.name,
                "description": brief.description,
                "keywords": brief.keywords,
            }
            for brief in self._scenario_loader.scenario_briefs
        ]

 # 当前激活的专业场景
        expert_scenario_info = None
        if session.expert_scenario:
            expert_rt = session.expert_scenario
            scenario_id = expert_rt.id
            if expert_rt.domain_meta:
                meta = expert_rt.domain_meta.get("meta", {})
                expert_scenario_info = {
                    "id": meta.get("id", scenario_id),
                    "name": meta.get("name", scenario_id),
                    "description": meta.get("description", ""),
                }
            else:
                expert_scenario_info = {
                    "id": scenario_id,
                    "name": scenario_id,
                    "description": "",
                }

        return {
            "scenarioBriefs": scenario_briefs,
            "expertScenario": expert_scenario_info,
        }

    def _build_task_section(self, active_task: ActiveTask | None) -> dict[str, Any]:
 """构建任务信息部分。"""
        if not active_task:
            return {"activeTask": None}

 # 构建 Partner 子任务摘要
        partner_summaries = []
        for partner_task in active_task.partner_tasks.values:
 # partner_name 字段可能不存在，fallback 到 partner_aic
            partner_name = getattr(partner_task, "partner_name", None) or partner_task.partner_aic
            summary: dict[str, Any] = {
                "partnerAic": partner_task.partner_aic,
                "partnerName": partner_name,
                "state": (partner_task.state.value if hasattr(partner_task.state, "value") else partner_task.state),
            }

 # 如果有缺口字段（missing_fields 可能不存在于某些版本的 PartnerTask）
            missing_fields = getattr(partner_task, "missing_fields", None)
            if missing_fields:
                summary["awaitingInputGaps"] = [
                    {
                        "field": f.field,
                        "description": f.description,
                        "required": f.required,
                    }
                    for f in missing_fields
                ]

            partner_summaries.append(summary)

        return {
            "activeTask": {
                "activeTaskId": active_task.active_task_id,
                "externalStatus": (
                    active_task.external_status.value
                    if hasattr(active_task.external_status, "value")
                    else active_task.external_status
                ),
                "partnerTaskSummaries": partner_summaries,
            }
        }

    def _build_session_section(self, session: Session) -> dict[str, Any]:
 """构建会话信息部分。"""
        result: dict[str, Any] = {}

 # 用户上下文
        if session.user_context:
            result["userContext"] = session.user_context

 # 对话上下文
        if session.dialog_context:
            dc = session.dialog_context

 # 最近 N 轮对话
            recent_turns = []
            for turn in dc.recent_turns[-RECENT_TURNS_LIMIT:]:
                recent_turns.append(
                    {
                        "userQuery": turn.user_query,
                        "intentType": turn.intent_type.value,
                        "responseType": turn.response_type.value,
                        "responseSummary": turn.response_summary,
                        "timestamp": turn.timestamp,
                    }
                )

            result["dialogContext"] = {
                "recentTurns": recent_turns,
                "historySummary": dc.history_summary if dc.history_summary else None,
            }

 # 用户结果状态
        if session.user_result:
            ur = session.user_result
            data_items_output = []
            for item in ur.data_items or []:
                item_dict = {"type": item.type}
 # 根据 DataItem 类型提取内容
                if hasattr(item, "text"):
                    item_dict["text"] = item.text
                elif hasattr(item, "data"):
                    item_dict["data"] = item.data
                elif hasattr(item, "uri"):
                    item_dict["uri"] = item.uri
                data_items_output.append(item_dict)

            result["userResult"] = {
                "type": ur.type.value if ur.type else None,
                "dataItems": data_items_output,
            }
        else:
            result["userResult"] = {"type": None}

        return result

    def _get_system_prompt(self, scenario_id: str | None) -> str:
 """获取意图分析的系统 prompt。"""
 # 获取 persona
        persona = self._scenario_loader.get_persona_system(scenario_id)

 # 获取 intent_analysis.system prompt
        intent_prompt = self._scenario_loader.get_prompt(
            "intent_analysis",
            "system",
            scenario_id,
        )

        if not intent_prompt:
            raise LLMCallError("intent_analysis.system prompt not found")

 # 合并 persona 和 intent_analysis prompt
        if persona:
            return f"{persona}\n\n{intent_prompt}"
        return intent_prompt

    def _validate_and_normalize(
        self,
        result: IntentDecision,
        session: Session,
        active_task: ActiveTask | None,
    ) -> IntentDecision:
 """验证并规范化意图决策结果。"""

 # 规则 1: TASK_INPUT 必须有 activeTask
        if result.intent_type == IntentType.TASK_INPUT:
            if not active_task:
                logger.warning("TASK_INPUT without activeTask, fallback to TASK_NEW")
                return IntentDecision(
                    intent_type=IntentType.TASK_NEW,
                    target_scenario=result.target_scenario,
                    task_instruction=result.task_instruction,
                    response_guide=None,
                )

 # 规则 2: CHIT_CHAT 不应有 taskInstruction
        if result.intent_type == IntentType.CHIT_CHAT:
            if result.task_instruction:
                result = IntentDecision(
                    intent_type=result.intent_type,
                    target_scenario=None,
                    task_instruction=None,
                    response_guide=result.response_guide or "继续与用户进行友好对话。",
                )

 # 规则 3: TASK_NEW/TASK_INPUT 必须有 taskInstruction
        if result.intent_type in (IntentType.TASK_NEW, IntentType.TASK_INPUT):
            if not result.task_instruction:
                result = IntentDecision(
                    intent_type=result.intent_type,
                    target_scenario=result.target_scenario,
                    task_instruction=TaskInstruction(
                        text="用户请求（待补充详情）",
                        data=None,
                    ),
                    response_guide=None,
                )

 # 规则 4: 验证 targetScenario 存在于已注册场景中
        if result.target_scenario:
            valid_ids = {b.id for b in self._scenario_loader.scenario_briefs}
            if result.target_scenario not in valid_ids:
                logger.warning(f"Unknown targetScenario: {result.target_scenario}, setting to None")
                result = IntentDecision(
                    intent_type=result.intent_type,
                    target_scenario=None,
                    task_instruction=result.task_instruction,
                    response_guide=result.response_guide,
                )

        return result

    def _apply_target_scenario_fallback(
        self,
        result: IntentDecision,
        user_query: str,
        session: Session,
    ) -> IntentDecision:
 """在 LLM 未返回 targetScenario 时使用关键词做唯一命中兜底。"""
        if result.intent_type != IntentType.TASK_NEW:
            return result

        if result.target_scenario or session.expert_scenario:
            return result

        inferred_scenario = self._infer_target_scenario_from_keywords(user_query)
        if not inferred_scenario:
            return result

        logger.info(
            "[LLM-1] Fallback targetScenario inferred from keywords: %s",
            inferred_scenario,
        )
        return IntentDecision(
            intent_type=result.intent_type,
            target_scenario=inferred_scenario,
            task_instruction=result.task_instruction,
            response_guide=result.response_guide,
        )

    def _infer_target_scenario_from_keywords(self, user_query: str) -> str | None:
 """基于场景关键词对 TASK_NEW 做兜底场景推断。"""
        query_text = user_query.casefold
        if not query_text.strip:
            return None

        scored_matches: list[tuple[str, int]] = []
        for brief in self._scenario_loader.scenario_briefs:
            score = sum(1 for keyword in brief.keywords if keyword and keyword.casefold in query_text)
            if score > 0:
                scored_matches.append((brief.id, score))

        if not scored_matches:
            return None

        scored_matches.sort(key=lambda item: item[1], reverse=True)
        if len(scored_matches) > 1 and scored_matches[0][1] == scored_matches[1][1]:
            logger.info(
                "[LLM-1] Scenario keyword fallback ambiguous: %s",
                scored_matches,
            )
            return None

        return scored_matches[0][0]


# 模块级工厂函数
def create_intent_analyzer(scenario_loader: ScenarioLoader) -> IntentAnalyzer:
 """创建意图分析器实例。"""
    return IntentAnalyzer(scenario_loader=scenario_loader)
