"""
Leader Agent Platform - Planner (LLM-2)

本模块实现 LLM-2 全量规划功能，包括：
- 维度与 Partner 匹配（静态优先 + 动态回退）
- 构建 LLM-2 输入上下文
- 调用 LLM 获取 PlanningResult
- 规划结果的后处理和验证
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

from ..llm.client import LLMClient, get_llm_client
from ..models import (
    DimensionNote,
    IntentDecision,
    LLMPlanningOutput,
    PartnerSelection,
    PlanningResult,
    Session,
    now_iso,
)
from ..models.exceptions import LLMCallError, LLMParseError
from ..services.discovery_client import (
    DiscoveryClient,
    DiscoveryClientError,
    get_discovery_client,
)
from ..services.scenario_loader import ScenarioLoader

logger = logging.getLogger(__name__)


PLANNING_LLM_MAX_ATTEMPTS = 2
PLANNING_PARSE_RETRY_COUNT = 1
PLANNING_RECENT_TURNS_LIMIT = 5


# =============================================================================
# Partner 候选信息（用于 LLM-2 输入）
# =============================================================================


class PartnerSkillInfo:
 """Partner Skill 信息。"""

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        tags: list[str] | None = None,
    ):
        self.id = id
        self.name = name
        self.description = description
        self.tags = tags or []

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
        }
        if self.tags:
            result["tags"] = self.tags
        return result


class PartnerCandidate:
 """Partner 候选信息。"""

    def __init__(
        self,
        partner_aic: str,
        partner_name: str,
        description: str,
        source: str,  # "static" or "dynamic"
        skills: list[PartnerSkillInfo],
    ):
        self.partner_aic = partner_aic
        self.partner_name = partner_name
        self.description = description
        self.source = source
        self.skills = skills

    def to_dict(self) -> dict[str, Any]:
        return {
            "partnerAic": self.partner_aic,
            "partnerName": self.partner_name,
            "description": self.description,
            "source": self.source,
            "skills": [s.to_dict for s in self.skills],
        }


# =============================================================================
# 维度定义（来自 domain.toml）
# =============================================================================


class DimensionDef:
 """维度定义。"""

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        fields: list[dict[str, Any]] | None = None,
    ):
        self.id = id
        self.name = name
        self.description = description
        self.fields = fields or []

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
        }
        if self.fields:
            result["fields"] = self.fields
        return result


# =============================================================================
# Planner 主类
# =============================================================================


class Planner:
 """
    LLM-2 全量规划器。

    负责：
    1. 维度与 Partner 匹配（Step 2）
    2. 构建 LLM-2 输入上下文
    3. 调用 LLM 生成 PlanningResult
    4. 后处理和验证
 """

    def __init__(
        self,
        scenario_loader: ScenarioLoader,
        llm_client: LLMClient | None = None,
        discovery_client: DiscoveryClient | None = None,
    ):
 """
        初始化规划器。

        Args:
            scenario_loader: 场景加载器
            llm_client: LLM 客户端（可选，默认使用单例）
            discovery_client: ADP 发现客户端（可选，默认使用单例）
 """
        self._scenario_loader = scenario_loader
        self._llm_client = llm_client or get_llm_client
        self._discovery_client = discovery_client or get_discovery_client
 # 缓存已加载的 ACS 文件
        self._acs_cache: dict[str, dict[str, Any]] = {}

    async def plan(
        self,
        user_query: str,
        session: Session,
        intent: IntentDecision,
    ) -> PlanningResult:
 """
        执行全量规划。

        流程：
        1. 加载场景维度配置
        2. 构建 dimensionPartnerMap
        3. 调用 LLM-2 生成规划结果
        4. 验证和后处理

        Args:
            user_query: 用户输入
            session: 当前会话
            intent: LLM-1 的意图决策结果

        Returns:
            PlanningResult 规划结果

        Raises:
            LLMCallError: LLM 调用失败
            LLMParseError: 响应解析失败
 """
 # 从 intent 或 session 获取场景 ID
        scenario_id = intent.target_scenario
        if not scenario_id and session.expert_scenario:
            scenario_id = session.expert_scenario.id
        if not scenario_id:
            raise LLMCallError("No scenario specified for planning")

        logger.info(f"Starting full planning for scenario: {scenario_id}")

 # 1. 加载场景配置
        scenario_runtime = self._scenario_loader.get_expert_scenario(scenario_id)
        if not scenario_runtime or not scenario_runtime.domain_meta:
            raise LLMCallError(f"Scenario not found or invalid: {scenario_id}")

 # 2. 提取维度定义
        dimensions = self._extract_dimensions(scenario_runtime.domain_meta)

 # 3. 构建 dimensionPartnerMap（Step 2）
        dimension_partner_map = await self._build_dimension_partner_map(
            scenario_id=scenario_id,
            dimensions=dimensions,
            domain_meta=scenario_runtime.domain_meta,
        )

 # 4. 构建 LLM-2 输入上下文
        input_context = self._build_input_context(
            user_query=user_query,
            intent=intent,
            scenario_id=scenario_id,
            scenario_runtime=scenario_runtime,
            dimensions=dimensions,
            dimension_partner_map=dimension_partner_map,
            session=session,
        )

 # 5. 获取 prompt
        system_prompt = self._scenario_loader.get_prompt("planning", "system", scenario_id)
        if not system_prompt:
            raise LLMCallError(f"Planning prompt not found for scenario: {scenario_id}")

        llm_profile = self._scenario_loader.get_llm_profile("planning", scenario_id)

 # 6. 构建用户消息
        input_json = json.dumps(input_context, ensure_ascii=False, indent=2)
        user_message = system_prompt.replace("{{input_json}}", input_json)

        logger.debug(f"[LLM-2] Input context size: {len(input_json)} chars, dimensions: {len(dimensions)}")
        logger.debug(f"[LLM-2] Using profile: {llm_profile}")

 # 7. 调用 LLM（使用 LLMPlanningOutput，只包含业务决策字段）
        import time

        start_time = time.time
        logger.debug("[LLM-2] >>> Starting LLM call for planning...")

        try:
            llm_output = self._call_planning_llm(
                llm_profile=llm_profile,
                user_message=user_message,
            )

            elapsed_ms = (time.time - start_time) * 1000
            logger.debug(f"[LLM-2] <<< LLM call completed in {elapsed_ms:.0f}ms")

 # 8. 系统填充元数据，转换为完整的 PlanningResult
            result = PlanningResult(
                created_at=now_iso,
                scenario_id=scenario_id,
                user_query=user_query[:500] if user_query else None,  # 截断存储
                selected_partners=llm_output.selected_partners,
                dimension_notes=llm_output.dimension_notes,
            )

            active_dims = [d for d, p in result.selected_partners.items if p]
            total_partners = sum(len(p) for p in result.selected_partners.values)
            logger.info(
                f"[LLM-2] Result: scenario={result.scenario_id}, "
                f"active_dimensions={active_dims}, partners={total_partners}, elapsed={elapsed_ms:.0f}ms"
            )

 # 9. 后处理验证
            return self._validate_and_normalize(
                result=result,
                dimensions=dimensions,
                dimension_partner_map=dimension_partner_map,
            )

        except LLMCallError, LLMParseError:
            raise
        except Exception as e:
            logger.error(f"Planning failed: {e}")
            raise LLMCallError(f"Planning failed: {e}")

    def _call_planning_llm(
        self,
        llm_profile: str,
        user_message: str,
    ) -> LLMPlanningOutput:
 """调用 LLM-2，并在结构化解析失败时做一次额外重试。"""
        last_error: LLMParseError | None = None

        for attempt in range(1, PLANNING_LLM_MAX_ATTEMPTS + 1):
            try:
                return self._llm_client.call_structured(
                    profile_name=llm_profile,
                    system_prompt="",  # prompt 已包含在 user_message 中
                    user_message=user_message,
                    response_model=LLMPlanningOutput,
                    temperature=0.3,
                    parse_retry_count=PLANNING_PARSE_RETRY_COUNT,
                )
            except LLMParseError as exc:
                last_error = exc
                logger.warning(
                    "[LLM-2] Structured planning output parse failed (attempt %s/%s): %s",
                    attempt,
                    PLANNING_LLM_MAX_ATTEMPTS,
                    exc,
                )
                if attempt < PLANNING_LLM_MAX_ATTEMPTS:
                    logger.info("[LLM-2] Retrying planning because the structured output was invalid JSON")

        if last_error is not None:
            raise last_error
        raise LLMParseError("Planning response parsing failed without details")

    def _extract_dimensions(
        self,
        domain_meta: dict[str, Any],
    ) -> list[DimensionDef]:
 """从 domain.toml 提取维度定义。"""
        dimensions_config = domain_meta.get("dimensions", {})
        dimensions = []

        for dim_id, dim_config in dimensions_config.items:
            if not isinstance(dim_config, dict):
                continue

 # 构建 fields 列表
            fields = []
            for field_type in ["required_fields", "optional_fields"]:
                field_names = dim_config.get(field_type, [])
                for field_name in field_names:
                    fields.append(
                        {
                            "name": field_name,
                            "type": "string",
                            "required": field_type == "required_fields",
                        }
                    )

            dimensions.append(
                DimensionDef(
                    id=dim_id,
                    name=dim_config.get("name", dim_id),
                    description=dim_config.get("description", ""),
                    fields=fields if fields else None,
                )
            )

        return dimensions

    async def _build_dimension_partner_map(
        self,
        scenario_id: str,
        dimensions: list[DimensionDef],
        domain_meta: dict[str, Any],
    ) -> dict[str, list[PartnerCandidate]]:
 """
        构建维度 → Partner 候选列表映射。

        始终优先使用静态配置：
                - 每个维度单独判断是否使用静态或动态候选。
                - 某个维度有可用的静态配置时，直接使用该维度的静态 ACS 候选。
                - 某个维度没有静态配置，或该维度的静态 ACS 都缺失/加载失败时，
                    仅该维度回退到动态发现。
 """
        result: dict[str, list[PartnerCandidate]] = {}

 # 静态配置：static_mapping 的值是 ACS 文件名数组
        partners_meta = domain_meta.get("partners", {})
        static_mapping = partners_meta.get("static_mapping") or partners_meta.get("mapping", {})

 # 场景目录路径
        scenario_path = self._scenario_loader.scenario_root / "expert" / scenario_id

 # 第一遍：逐维度处理静态配置，收集需要动态查询的维度
        dynamic_dims: list[DimensionDef] = []

        for dim in dimensions:
            dim_id = dim.id
            candidates: list[PartnerCandidate] = []

            acs_files = static_mapping.get(dim_id)
            if acs_files:
                file_list = [acs_files] if isinstance(acs_files, str) else acs_files
                logger.info(
                    f"[LLM-2] Dimension '{dim_id}': trying static config ({len(file_list)} ACS file(s): {file_list})"
                )
                for acs_filename in file_list:
                    partner_info = self._load_partner_acs_by_filename(scenario_path, acs_filename)
                    if partner_info:
                        candidates.append(partner_info)

            if candidates:
                logger.info(
                    f"[LLM-2] Dimension '{dim_id}': {len(candidates)} candidate(s) "
                    f"(static)"
                    + (f" - {[c.partner_name or c.partner_aic[:16] for c in candidates]}" if candidates else "")
                )
                result[dim_id] = candidates
            else:
                if acs_files:
                    logger.warning(
                        f"[LLM-2] Dimension '{dim_id}': static config yielded no usable "
                        f"candidate, falling back to discovery server"
                    )
                else:
                    logger.info(f"[LLM-2] Dimension '{dim_id}': no static config, will query discovery server")
                dynamic_dims.append(dim)

 # 第二遍：并行查询所有需要动态发现的维度
        if dynamic_dims:
            import asyncio

            logger.info(
                f"[LLM-2] Querying discovery server for {len(dynamic_dims)} "
                f"dimension(s) concurrently: {[d.id for d in dynamic_dims]}"
            )

            async def _query_one(dim: DimensionDef) -> tuple:
                candidates = await self._query_discovery_server(dimension=dim)
                return dim.id, candidates

            query_results = await asyncio.gather(
                *[_query_one(d) for d in dynamic_dims],
                return_exceptions=True,
            )

            for dim, query_result in zip(dynamic_dims, query_results):
                if isinstance(query_result, BaseException):
                    logger.error(f"[LLM-2] Dimension '{dim.id}': discovery query raised exception: {query_result}")
                    result[dim.id] = []
                else:
                    dim_id, candidates = cast("tuple[str, list[PartnerCandidate]]", query_result)
                    logger.info(
                        f"[LLM-2] Dimension '{dim_id}': {len(candidates)} candidate(s) "
                        f"(dynamic)"
                        + (f" - {[c.partner_name or c.partner_aic[:16] for c in candidates]}" if candidates else "")
                    )
                    result[dim_id] = candidates

        return result

    async def _query_discovery_server(
        self,
        dimension: DimensionDef,
    ) -> list[PartnerCandidate]:
 """
        通过 ADP 发现服务查询满足维度需求的 Partner。

        Args:
            dimension: 维度定义

        Returns:
            匹配的 PartnerCandidate 列表
 """
        if not self._discovery_client.is_configured:
            logger.debug(f"[ADP] Discovery server not configured, skip dynamic query for dimension: {dimension.id}")
            return []

        try:
            response = await self._discovery_client.discover_for_dimension(
                dimension_name=dimension.name,
                dimension_description=dimension.description,
            )

            if not response.is_success or not response.result:
                logger.warning(f"[ADP] Discovery returned no result for dimension: {dimension.id}")
                return []

            candidates = self._parse_discovery_response(response)
            logger.info(
                f"[ADP] Discovered {len(candidates)} candidate(s) "
                f"for dimension '{dimension.id}': "
                f"{[f'{c.partner_name}({c.partner_aic[:16]})' for c in candidates]}"
            )
            return candidates

        except DiscoveryClientError as e:
 # 利用 ADPError 分类做差异化日志
            if e.adp_error and e.adp_error.is_retryable:
                logger.warning(f"[ADP] Rate limited for dimension {dimension.id}, may retry later: {e}")
            elif e.adp_error and e.adp_error.is_redirect:
                logger.warning(f"[ADP] Redirect suggested for dimension {dimension.id}: {e}")
            else:
                logger.warning(f"[ADP] Discovery query failed for dimension {dimension.id}: {e}")
            return []

    def _parse_discovery_response(
        self,
        response,
    ) -> list[PartnerCandidate]:
 """
        将 ADP DiscoveryResponse 转换为 PartnerCandidate 列表。
 """
        candidates: list[PartnerCandidate] = []

        if not response.result or not response.result.agents:
            return candidates

 # 使用 SDK 提供的 iter_agent_skills 自动关联 acsMap
        for aic, acs_data, agent_skill, group in response.result.iter_agent_skills:
 # 缓存 ACS 数据供后续 executor 使用
            if aic and acs_data:
                self._acs_cache[aic] = acs_data

            candidate = self._parse_acs_to_candidate(
                acs_data={**acs_data, "aic": aic} if acs_data else {"aic": aic},
                source="dynamic",
            )
            candidates.append(candidate)

 # 去重：同一 AIC 只保留第一个
        seen_aics: set = set
        unique: list[PartnerCandidate] = []
        for c in candidates:
            if c.partner_aic and c.partner_aic not in seen_aics:
                seen_aics.add(c.partner_aic)
                unique.append(c)
        return unique

    def _load_partner_acs_by_filename(
        self,
        scenario_path: Path,
        acs_filename: str,
    ) -> PartnerCandidate | None:
 """
        根据文件名加载 Partner ACS 文件。

        Args:
            scenario_path: 场景目录路径
            acs_filename: ACS 文件名（如 "china_hotel.json"）

        Returns:
            PartnerCandidate 或 None（加载失败时）
 """
 # 检查缓存
        cache_key = f"{scenario_path}:{acs_filename}"
        if cache_key in self._acs_cache:
            acs_data = self._acs_cache[cache_key]
            return self._parse_acs_to_candidate(acs_data, "static")

 # 构建完整路径
        acs_file = scenario_path / acs_filename

        if not acs_file.exists:
            logger.warning(f"ACS file not found: {acs_file}")
            return None

        try:
            with open(acs_file, encoding="utf-8") as f:
                acs_data = json.load(f)

 # 缓存：同时以文件路径和 partner_aic 为 key
            self._acs_cache[cache_key] = acs_data
 # 以 partner_aic 为 key 缓存，供 executor 使用
            partner_aic = acs_data.get("aic")
            if partner_aic:
                self._acs_cache[partner_aic] = acs_data

            return self._parse_acs_to_candidate(acs_data, "static")
        except Exception as e:
            logger.warning(f"Failed to load ACS file {acs_file}: {e}")
            return None

    def _parse_acs_to_candidate(
        self,
        acs_data: dict[str, Any],
        source: str,
    ) -> PartnerCandidate:
 """将 ACS 数据转换为 PartnerCandidate。"""
        skills = []
        for skill_data in acs_data.get("skills", []):
            skills.append(
                PartnerSkillInfo(
                    id=skill_data.get("id", ""),
                    name=skill_data.get("name", ""),
                    description=skill_data.get("description", ""),
                    tags=skill_data.get("tags", []),
                )
            )

        return PartnerCandidate(
            partner_aic=acs_data.get("aic", ""),
            partner_name=acs_data.get("name", ""),
            description=acs_data.get("description", ""),
            source=source,
            skills=skills,
        )

    def _build_input_context(
        self,
        user_query: str,
        intent: IntentDecision,
        scenario_id: str,
        scenario_runtime: Any,
        dimensions: list[DimensionDef],
        dimension_partner_map: dict[str, list[PartnerCandidate]],
        session: Session,
    ) -> dict[str, Any]:
 """构建 LLM-2 输入上下文。"""
 # 场景信息
        meta = scenario_runtime.domain_meta.get("meta", {})

 # 构建 dimensionPartnerMap（转换为 JSON 格式）
        dim_partner_map_json = {
            dim_id: [c.to_dict for c in candidates] for dim_id, candidates in dimension_partner_map.items
        }

        context = {
            "userQuery": user_query,
            "taskInstruction": {
                "scenarioId": scenario_id,
                "taskSummary": (intent.task_instruction.text if intent.task_instruction else None),
            },
            "scenario": {
                "expertScenario": {
                    "id": meta.get("id", scenario_id),
                    "name": meta.get("name", scenario_id),
                    "description": meta.get("description", ""),
                    "dimensions": [d.to_dict for d in dimensions],
                }
            },
            "dimensionPartnerMap": dim_partner_map_json,
        }

 # 添加 session 上下文
        user_context = session.user_context if session.user_context else None
        dialog_context = session.dialog_context if session.dialog_context else None

        if user_context or dialog_context:
            session_ctx = {}
            if user_context:
                session_ctx["userContext"] = user_context
            if dialog_context:
                dialog_ctx: dict[str, Any] = {}
                if dialog_context.recent_turns:
                    dialog_ctx["recentTurns"] = [
                        {
                            "userQuery": turn.user_query,
                            "intentType": turn.intent_type.value,
                            "responseType": turn.response_type.value,
                            "responseSummary": turn.response_summary,
                            "timestamp": turn.timestamp,
                        }
                        for turn in dialog_context.recent_turns[-PLANNING_RECENT_TURNS_LIMIT:]
                    ]
                if dialog_context.history_summary:
                    dialog_ctx["historySummary"] = dialog_context.history_summary
                if dialog_ctx:
                    session_ctx["dialogContext"] = dialog_ctx
            context["session"] = session_ctx

        return context

    def _validate_and_normalize(
        self,
        result: PlanningResult,
        dimensions: list[DimensionDef],
        dimension_partner_map: dict[str, list[PartnerCandidate]],
    ) -> PlanningResult:
 """
        验证和规范化规划结果。

        验证规则：
        1. selectedPartners 必须覆盖所有维度
        2. 选中的 Partner AIC 必须在候选列表中
        3. 选中的 Skill ID 必须存在于该 Partner 的 skills 中
 """
        dim_ids = {d.id for d in dimensions}

 # 确保所有维度都有对应的 key
        for dim_id in dim_ids:
            if dim_id not in result.selected_partners:
                result.selected_partners[dim_id] = []

 # 添加 dimensionNotes
                if result.dimension_notes is None:
                    result.dimension_notes = {}
                result.dimension_notes[dim_id] = DimensionNote(
                    status="inactive",
                    reason="LLM-2 未返回该维度的规划",
                )

        if sum(len(selections) for selections in result.selected_partners.values) == 0:
            self._apply_empty_selection_fallback(result, dimensions, dimension_partner_map)

        partner_candidate_map = self._build_partner_candidate_map(dimension_partner_map)
        self._normalize_duplicate_partner_assignments(result, partner_candidate_map)

 # 验证选中的 Partner 和 Skill
        for dim_id, selections in result.selected_partners.items:
            candidates = dimension_partner_map.get(dim_id, [])
            candidate_map = {c.partner_aic: c for c in candidates}

            for selection in selections:
 # 验证 Partner AIC
                if selection.partner_aic not in candidate_map:
                    logger.warning(f"Selected Partner {selection.partner_aic} not in candidates for dimension {dim_id}")
                    continue

 # 验证 Skill ID
                candidate = candidate_map[selection.partner_aic]
                skill_ids = {s.id for s in candidate.skills}
                if selection.skill_id not in skill_ids:
                    logger.warning(
                        f"Selected Skill {selection.skill_id} not found in Partner "
                        f"{selection.partner_aic} for dimension {dim_id}"
                    )

 # 设置 created_at（如果 LLM 未返回有效值）
        if not result.created_at or result.created_at == "":
            result.created_at = now_iso

        return result

    def _apply_empty_selection_fallback(
        self,
        result: PlanningResult,
        dimensions: list[DimensionDef],
        dimension_partner_map: dict[str, list[PartnerCandidate]],
    ) -> None:
 """当 LLM-2 返回空规划时，回退到每个维度的首个可用候选。"""
        if result.dimension_notes is None:
            result.dimension_notes = {}

        fallback_count = 0

        for dim in dimensions:
            candidates = dimension_partner_map.get(dim.id, [])
            if not candidates:
                continue

            candidate = candidates[0]
            if not candidate.skills:
                logger.warning(
                    "[LLM-2] Empty-planning fallback skipped for dimension '%s': candidate %s has no skills",
                    dim.id,
                    candidate.partner_aic,
                )
                continue

            primary_skill = candidate.skills[0]
            result.selected_partners[dim.id] = [
                PartnerSelection(
                    partner_aic=candidate.partner_aic,
                    skill_id=primary_skill.id,
                    skill_name=primary_skill.name,
                    reason="LLM-2 未返回可执行规划，系统回退到默认候选",
                    instruction_text=(
                        f"请聚焦处理“{dim.name}”相关需求，并基于用户原始请求给出专业建议：{result.user_query or ''}"
                    ),
                    acs_data=self._get_candidate_acs_data(candidate),
                )
            ]
            result.dimension_notes[dim.id] = DimensionNote(
                status="active",
                reason="LLM-2 未返回可执行规划，系统已自动选择默认候选",
            )
            fallback_count += 1

        if fallback_count:
            logger.warning(
                "[LLM-2] Applied empty-planning fallback: scenario=%s, selected_dimensions=%d",
                result.scenario_id,
                fallback_count,
            )

    def _get_candidate_acs_data(self, candidate: PartnerCandidate) -> dict[str, Any] | None:
 """从 ACS 缓存中查找候选 Partner 的原始 ACS 数据。"""
        for acs_data in self._acs_cache.values:
            if acs_data.get("aic") == candidate.partner_aic:
                return acs_data
        return None

    def _build_partner_candidate_map(
        self,
        dimension_partner_map: dict[str, list[PartnerCandidate]],
    ) -> dict[str, PartnerCandidate]:
 """构建 partner AIC 到候选信息的索引。"""
        candidate_map: dict[str, PartnerCandidate] = {}
        for candidates in dimension_partner_map.values:
            for candidate in candidates:
                candidate_map.setdefault(candidate.partner_aic, candidate)
        return candidate_map

    def _normalize_duplicate_partner_assignments(
        self,
        result: PlanningResult,
        partner_candidate_map: dict[str, PartnerCandidate],
    ) -> None:
 """对同一 Partner 被多个维度复用的规划结果做归一化。"""
        assignments: dict[str, dict[str, Any]] = {}

        for dim_id, selections in result.selected_partners.items:
            for selection in selections:
                item = assignments.setdefault(
                    selection.partner_aic,
                    {"dimensions": [], "selections": []},
                )
                if dim_id not in item["dimensions"]:
                    item["dimensions"].append(dim_id)
                item["selections"].append(selection)

        for partner_aic, item in assignments.items:
            selections = cast("list[PartnerSelection]", item["selections"])
            if len(selections) <= 1:
                continue

            candidate = partner_candidate_map.get(partner_aic)
            merged_instruction = self._build_partner_instruction(
                scenario_id=result.scenario_id,
                user_query=result.user_query or "",
                candidate=candidate,
                dimensions=cast("list[str]", item["dimensions"]),
                selections=selections,
            )
            if not merged_instruction:
                continue

            for selection in selections:
                selection.instruction_text = merged_instruction

    def _build_partner_instruction(
        self,
        scenario_id: str | None,
        user_query: str,
        candidate: PartnerCandidate | None,
        dimensions: list[str],
        selections: list[PartnerSelection],
    ) -> str:
 """为同一 Partner 的多维规划生成统一指令。"""
        instruction_texts = self._dedupe_texts([selection.instruction_text for selection in selections])
        if not instruction_texts:
            return ""

        if scenario_id != "tour" or not candidate:
            return "\n".join(instruction_texts)

        partner_name = candidate.partner_name
        if partner_name == "北京城区旅游智能体":
            return self._build_tour_geo_instruction(
                region="urban",
                user_query=user_query,
                dimensions=dimensions,
                instruction_texts=instruction_texts,
            )
        if partner_name == "北京郊区旅游智能体":
            return self._build_tour_geo_instruction(
                region="rural",
                user_query=user_query,
                dimensions=dimensions,
                instruction_texts=instruction_texts,
            )

        return "\n".join(instruction_texts)

    def _build_tour_geo_instruction(
        self,
        region: str,
        user_query: str,
        dimensions: list[str],
        instruction_texts: list[str],
    ) -> str:
 """为 tour 场景的城区/郊区 Partner 构造不越界的专项任务。"""
        fragments = self._split_instruction_fragments(instruction_texts)
        filtered_fragments = [fragment for fragment in fragments if self._fragment_matches_region(fragment, region)]
        if not filtered_fragments:
            filtered_fragments = fragments or self._split_instruction_fragments([user_query])

        focus_text = "；".join(filtered_fragments)
        has_attraction = "attraction" in dimensions
        has_local_transport = "local_transport" in dimensions

        if region == "urban":
            tasks: list[str] = []
            if has_attraction:
                tasks.append("给出北京城六区内景点的游览顺序、停留重点和体验建议")
            if has_local_transport:
                tasks.append("补充这些城区景点之间的步行、地铁或打车等轻量交通衔接建议")

            task_text = "；".join(tasks) if tasks else "提供北京城六区内的游览建议"
            return (
                f"请围绕北京城六区行程提供建议：{focus_text}。"
                f"重点任务：{task_text}。"
                "仅处理故宫、王府井、天安门等城六区范围内容，忽略郊区景点、跨城交通、餐饮和酒店部分。"
            )

        tasks = []
        if has_attraction:
            tasks.append("给出北京郊区景点的游览安排、顺序和注意事项")
        if has_local_transport:
            tasks.append("可补充景区内或景区周边的轻量交通衔接提示，但不要提供城区到景区的通勤方案")

        task_text = "；".join(tasks) if tasks else "提供北京郊区景点游览建议"
        return (
            f"请围绕北京郊区行程提供建议：{focus_text}。"
            f"重点任务：{task_text}。"
            "仅处理八达岭长城、延庆、怀柔等郊区范围内容，忽略城六区景点、跨城交通、餐饮和酒店部分。"
        )

    def _split_instruction_fragments(self, instruction_texts: list[str]) -> list[str]:
 """按中文语义边界拆分指令片段，并去重。"""
        raw_fragments: list[str] = []
        for text in instruction_texts:
            normalized = re.sub(r"[\n。！？!?,，；;]", "；", text)
            normalized = re.sub(r"(?:并且|并|以及|同时|另外|再|也想|还想)", "；", normalized)
            for fragment in normalized.split("；"):
                cleaned = fragment.strip(" ，,；。")
                if cleaned:
                    raw_fragments.append(cleaned)
        return self._dedupe_texts(raw_fragments)

    def _fragment_matches_region(self, fragment: str, region: str) -> bool:
 """判断指令片段是否属于城区或郊区范围。"""
        urban_keywords = [
            "故宫",
            "王府井",
            "天安门",
            "景山",
            "天坛",
            "颐和园",
            "胡同",
            "城区",
            "东城",
            "西城",
            "朝阳",
            "海淀",
            "丰台",
            "石景山",
        ]
        rural_keywords = [
            "八达岭",
            "长城",
            "延庆",
            "怀柔",
            "密云",
            "昌平",
            "门头沟",
            "房山",
            "大兴",
            "顺义",
            "平谷",
            "通州",
            "郊区",
        ]

        keywords = urban_keywords if region == "urban" else rural_keywords
        return any(keyword in fragment for keyword in keywords)

    def _dedupe_texts(self, texts: list[str]) -> list[str]:
 """保序去重并过滤空字符串。"""
        seen: set[str] = set
        deduped: list[str] = []
        for text in texts:
            cleaned = text.strip
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        return deduped


# =============================================================================
# 单例获取
# =============================================================================

_planner_instance: Planner | None = None


def get_planner(scenario_loader: ScenarioLoader | None = None) -> Planner:
 """获取 Planner 单例。"""
    global _planner_instance
    if _planner_instance is None:
        if scenario_loader is None:
            raise ValueError("ScenarioLoader must be provided for first initialization")
        _planner_instance = Planner(scenario_loader)
    return _planner_instance
