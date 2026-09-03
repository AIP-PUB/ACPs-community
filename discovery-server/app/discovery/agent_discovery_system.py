from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

import numpy as np
from numpy.typing import NDArray
from openai import AsyncOpenAI, OpenAIError
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import Integer, Text

from app.core.config import settings
from app.core.database import get_async_session_context
from app.core.logging_config import get_logger
from app.discovery.schema import DiscoveryFilters

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.discovery.semantic_matcher import SemanticAgentMatcher

logger = get_logger(__name__)

DISCOVERY_PIPELINE_ERRORS = (
    SQLAlchemyError,
    OpenAIError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
)

SQL_AND = " AND "


class OptimizationConfig(TypedDict):
    coarse_k: int
    relevance_threshold: float
    limit_skills_per_agent: bool
    max_skills_per_agent: int
    cpu_llm_candidate_k: int


class OptimizationConfigUpdate(TypedDict, total=False):
    coarse_k: int
    relevance_threshold: float
    limit_skills_per_agent: bool
    max_skills_per_agent: int
    cpu_llm_candidate_k: int


type DenseVector = NDArray[np.float32]
type SparseVector = dict[Any, float]
type SkillResult = dict[str, Any]


class EnhancedAgentDiscoverySystem:
    """增强的智能体发现系统"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
        prompt_file_path: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        # 组件初始化
        # 源码树：__file__ = <repo>/app/discovery/... → parents[2] = <repo>
        # wheel 安装：__file__ 落在 site-packages/app/discovery/... → parents[2]
        # 是 site-packages（root 所有、服务用户不可写）。host/image 均以
        # WorkingDirectory / WORKDIR 为可写根（app-release 落地目录）。
        _here = Path(__file__).resolve()
        project_root = Path.cwd() if "site-packages" in _here.parts else _here.parents[2]
        # 数据日志目录初始化
        self.data_log_dir = project_root / "logs" / "data_log"
        self.data_log_dir.mkdir(parents=True, exist_ok=True)
        logger.info("数据日志目录已就绪", data_log_dir=str(self.data_log_dir))
        self.api_key = api_key or settings.DISCOVERY_LLM_API_KEY
        self.base_url = base_url or settings.DISCOVERY_LLM_BASE_URL
        self.model_name = model_name or settings.DISCOVERY_LLM_MODEL_NAME or "qwen-plus"
        self.client: AsyncOpenAI | None = None
        if self.api_key and self.base_url:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=120.0,
            )
        # 优化配置
        self.optimization_config: OptimizationConfig = {
            "coarse_k": 50,  # 粗排数量（从数据库/向量库取出的初步候选）
            "relevance_threshold": 0.05,  # 相关性阈值
            "limit_skills_per_agent": True,  # 是否限制单个智能体的技能数量
            "max_skills_per_agent": 2,  # 单个智能体最多进入重排序的技能数
            "cpu_llm_candidate_k": 10,  # CPU模式送入LLM终选的最大候选数
        }
        prompt_file_path = prompt_file_path or ["", ""]
        self._prompt = self.load_prompt_from_file(prompt_file_path[0])
        self.cluster_prompt = self.load_prompt_from_file(prompt_file_path[-1])
        self.semantic_matcher: SemanticAgentMatcher | None = None

    def _limit_skills_per_agent(self, skills: list[dict[str, Any]], max_per_agent: int) -> list[dict[str, Any]]:
        """
        限制单个智能体进入重排序的技能数量，防止恶意刷榜

        Args:
            skills: 技能列表（已按RRF分数排序）
            max_per_agent: 每个智能体最多保留的技能数

        Returns:
            过滤后的技能列表
        """
        from collections import defaultdict

        agent_skill_count: defaultdict[str, int] = defaultdict(int)
        filtered_skills = []

        for skill in skills:
            aic = skill.get("aic", "")

            # 检查该智能体的技能数是否已达上限
            if agent_skill_count[aic] < max_per_agent:
                filtered_skills.append(skill)
                agent_skill_count[aic] += 1

        # 统计信息
        total_agents = len(agent_skill_count)
        filtered_count = len(skills) - len(filtered_skills)

        if filtered_count > 0:
            logger.warning(
                "技能过滤已生效",
                total_agents=total_agents,
                filtered_count=filtered_count,
                max_per_agent=max_per_agent,
            )

        return filtered_skills

    def load_prompt_from_file(self, file_path: str) -> str:
        """
        从指定文件路径读取并返回整个 Prompt 内容。
        """
        try:
            if not file_path:
                return ""

            with Path(file_path).open(encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            logger.error("文件未找到", file_path=file_path)
            return ""
        except OSError as e:
            logger.error("读取文件失败", file_path=file_path, error=str(e))
            return ""

    async def _call_llm_api(self, query: str) -> str:
        """调用 Discovery LLM 进行任务分解。"""
        if not self.api_key:
            raise ValueError("API密钥未设置，请配置 DISCOVERY_LLM_API_KEY")
        if not self._prompt:
            raise ValueError("未设置Planner提示词")

        prompt = self._prompt.format(self.cluster_prompt, query)
        logger.info("正在调用 Discovery LLM", model_name=self.model_name)

        if self.base_url:
            if self.client is None:
                self.client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=120.0,
                )

            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                temperature=0.1,
            )

            if not response.choices:
                raise RuntimeError("Discovery LLM 响应格式异常")
            content = response.choices[0].message.content or ""
            if not content:
                raise RuntimeError("Discovery LLM 响应内容为空")

            logger.info("收到 Discovery LLM 响应", content_length=len(content))
            return content

        # 兼容旧版：未配置 DISCOVERY_LLM_BASE_URL 时给出明确提示
        raise ValueError("未配置 DISCOVERY_LLM_BASE_URL，无法调用 Discovery LLM")

    def _save_discovery_log(self, log_data: dict[str, Any], save_full: bool = False) -> str | None:
        """保存发现过程的统计日志到本地文件

        Args:
            log_data: 完整日志数据
            save_full: 是否保存完整日志（默认False，只保存精简版）
        """
        try:
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")

            if save_full:
                # 完整日志（可选）
                log_file = self.data_log_dir / f"discovery_log_full_{timestamp}.json"
                with log_file.open("w", encoding="utf-8") as f:
                    json.dump(log_data, f, ensure_ascii=False, indent=2)
            else:
                # 精简日志（默认）- 适配新的混合检索流程
                compact_log = {
                    "timestamp": log_data["request_info"]["timestamp"],
                    "query": log_data["request_info"]["original_query"],
                    "limit": log_data["request_info"]["requested_count"],
                    "stats": {
                        "dense_retrieval": log_data["process_info"]["dense_retrieval_count"],
                        "sparse_retrieval": log_data["process_info"]["sparse_retrieval_count"],
                        "rrf_fusion": log_data["process_info"]["rrf_fusion_count"],
                        "rerank": log_data["process_info"]["rerank_count"],
                        "agent_reconstruction": log_data["process_info"]["agent_reconstruction_count"],
                        "final_returned": log_data["process_info"]["final_returned_count"],
                        "relevance_threshold": log_data["process_info"]["relevance_threshold"],
                    },
                    "performance": {
                        "total_time_ms": log_data["performance_metrics"]["total_time"],
                        "step1_embedding_ms": log_data["performance_metrics"]["step_times"].get("step1_embedding", 0),
                        "step2_retrieval_ms": log_data["performance_metrics"]["step_times"].get("step2_retrieval", 0),
                        "step3_rrf_fusion_ms": log_data["performance_metrics"]["step_times"].get("step3_rrf_fusion", 0),
                        "step4_rerank_ms": log_data["performance_metrics"]["step_times"].get("step4_rerank", 0),
                        "step5_reconstruct_ms": log_data["performance_metrics"]["step_times"].get(
                            "step5_reconstruct", 0
                        ),
                        "step6_format_ms": log_data["performance_metrics"]["step_times"].get("step6_format", 0),
                    },
                    "intermediate_results": {
                        "dense_top10": log_data["intermediate_results"].get("dense_top10", []),
                        "sparse_top10": log_data["intermediate_results"].get("sparse_top10", []),
                        "rrf_top10": log_data["intermediate_results"].get("rrf_top10", []),
                        "rerank_top10": log_data["intermediate_results"].get("rerank_top10", []),
                        "agent_top10": log_data["intermediate_results"].get("agent_top10", []),
                    },
                    "final_results": log_data["final_results"],
                    "config": log_data.get("config", {}),
                    "error": log_data.get("error"),
                }

                log_file = self.data_log_dir / f"discovery_log_{timestamp}.json"
                with log_file.open("w", encoding="utf-8") as f:
                    json.dump(compact_log, f, ensure_ascii=False, indent=2)

            logger.info("统计日志已保存", log_file_name=log_file.name)
            return str(log_file)
        except (OSError, TypeError, ValueError) as e:
            logger.exception("保存统计日志失败", error=str(e))
            return None

    def _extract_agent_url(self, agent: dict[str, Any]) -> str:
        """从新的ACS结构中提取Agent的URL"""

        endpoints = agent.get("endPoints", [])
        if endpoints and len(endpoints) > 0:
            endpoint = endpoints[0]
            if isinstance(endpoint, dict):
                endpoint_url = endpoint.get("url")
                if isinstance(endpoint_url, str):
                    return endpoint_url

        web_app_url = agent.get("webAppUrl", "")
        if isinstance(web_app_url, str) and web_app_url:
            return web_app_url

        doc_url = agent.get("documentationUrl", "")
        if isinstance(doc_url, str) and doc_url:
            return doc_url

        return ""

    def _expand_agents_to_skills(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把Agent展开为Skill候选列表，增强版本

        处理逻辑：
        1. 有技能的Agent：展开为多个skill候选
        2. 无技能的Agent：转换为单个skill候选（继承Agent描述）
        """
        skill_candidates = []

        for agent in agents:
            # 提取Agent公共信息
            agent_aic = agent.get("aic", "")
            agent_url = self._extract_agent_url(agent)
            agent_name = agent.get("name", "")
            agent_description = agent.get("description", "")
            agent_status = "active" if agent.get("active", False) else "inactive"
            provider_info = agent.get("provider", {})

            skills = agent.get("skills", [])

            # 关键改进：处理空技能列表
            if not skills:
                # 将Agent本身作为一个skill候选
                skill_candidate = {
                    # 基础信息
                    "aic": agent_aic,
                    "url": agent_url,
                    "skillid": "",
                    "skill_name": "",
                    "description": agent_description,
                    # 技能详细信息（使用默认值）
                    "tags": agent.get("tags", []),
                    "inputTypes": [],
                    "outputTypes": [],
                    "version": agent.get("version", ""),
                    "examples": [],
                    # Agent信息
                    "agent_name": agent_name,
                    "agent_description": agent_description,
                    "agent_status": agent_status,
                    "provider": provider_info,
                    # 标记为Agent级能力
                    "parent_agent": agent,
                    "is_agent_level_skill": True,  # 新增标记字段
                }
                skill_candidates.append(skill_candidate)
            else:
                # 原有逻辑：展开具体技能
                for skill in skills:
                    skill_candidate = {
                        # 基础信息
                        "aic": agent_aic,
                        "url": agent_url,
                        "skillid": skill.get("id", ""),
                        "skill_name": skill.get("name", ""),
                        "description": skill.get("description", ""),
                        # 技能详细信息
                        "tags": skill.get("tags", []),
                        "inputTypes": skill.get("inputModes", []),
                        "outputTypes": skill.get("outputModes", []),
                        "version": skill.get("version", ""),
                        "examples": skill.get("examples", []),
                        # Agent信息
                        "agent_name": agent_name,
                        "agent_description": agent_description,
                        "agent_status": agent_status,
                        "provider": provider_info,
                        # 完整Agent信息
                        "parent_agent": agent,
                        "is_agent_level_skill": False,
                    }
                    skill_candidates.append(skill_candidate)

        return skill_candidates

    def update_optimization_config(self, **kwargs: Unpack[OptimizationConfigUpdate]) -> None:
        """更新优化配置"""
        self.optimization_config.update(kwargs)

        logger.info("优化配置已更新", optimization_config=self.optimization_config)

    def _require_semantic_matcher(self) -> SemanticAgentMatcher:
        matcher = self.semantic_matcher
        if matcher is None:
            raise RuntimeError("语义匹配器未初始化")
        return matcher

    async def _discovery_agents_async(
        self,
        query: str,
        limit: int = 5,
        filters: DiscoveryFilters | None = None,
        query_type: str = "explicit",
    ) -> list[dict[str, Any]]:
        """
        基于混合检索发现智能体（支持过滤）

        Args:
            query: 查询文本
            limit: 最终返回的结果数量
            filters: 过滤条件（可选）
            query_type: 查询类型 (explicit/filtered)

        Returns:
            发现的智能体列表
        """
        from app.discovery.semantic_matcher_holder import get_matcher

        self.semantic_matcher = get_matcher()
        self._require_semantic_matcher()

        total_start = time.perf_counter()
        is_cpu_explicit = query_type == "explicit_cpu"
        log_data: dict[str, Any] | None = None

        try:
            request_time = datetime.now(UTC).isoformat()
            log_data = self._initialize_discovery_log(query, limit, filters, query_type, request_time)

            query_dense, query_sparse = await self._generate_query_embeddings(query, is_cpu_explicit, log_data)
            dense_results, sparse_results = await self._retrieve_candidates(
                query_dense,
                query_sparse,
                filters,
                is_cpu_explicit,
                log_data,
            )

            if not dense_results and not sparse_results:
                logger.warning("检索结果为空，可能是过滤条件过严")
                log_data["performance_metrics"]["total_time"] = (time.perf_counter() - total_start) * 1000
                self._save_discovery_log(log_data)
                return []

            final_results, rerank_top_k = await self._rank_candidates(
                query,
                dense_results,
                sparse_results,
                is_cpu_explicit,
                log_data,
            )
            filtered_skills, relevance_threshold = self._filter_relevant_skills(
                final_results,
                is_cpu_explicit,
                rerank_top_k,
                log_data,
            )

            if is_cpu_explicit:
                import os as _os

                if (_os.environ.get("DISCOVERY_SKIP_CPU_LLM") or "").strip() in {
                    "1",
                    "true",
                    "TRUE",
                    "yes",
                }:
                    logger.info("步骤5.5 跳过 CPU LLM 终选（DISCOVERY_SKIP_CPU_LLM）")
                    log_data["process_info"]["cpu_llm_applied"] = False
                    log_data["process_info"]["cpu_llm_skipped"] = True
                else:
                    filtered_skills, _ = await self._apply_cpu_llm_final_selection(
                        query, filtered_skills, limit, log_data
                    )

            result = self._format_discovery_results(filtered_skills, limit, log_data)
            self._finalize_discovery_success(log_data, result, total_start, is_cpu_explicit, relevance_threshold)
            return result
        except DISCOVERY_PIPELINE_ERRORS as error:
            self._record_discovery_failure(error, log_data, total_start)
            raise

    def _initialize_discovery_log(
        self,
        query: str,
        limit: int,
        filters: DiscoveryFilters | None,
        query_type: str,
        request_time: str,
    ) -> dict[str, Any]:
        """初始化发现流程日志结构。"""

        return {
            "request_info": {
                "timestamp": request_time,
                "original_query": query,
                "requested_count": limit,
                "has_filters": filters is not None,
                "query_type": query_type,
            },
            "process_info": {
                "dense_retrieval_count": 0,
                "sparse_retrieval_count": 0,
                "rrf_fusion_count": 0,
                "rerank_count": 0,
                "agent_reconstruction_count": 0,
                "final_returned_count": 0,
                "relevance_threshold": self.optimization_config["relevance_threshold"],
            },
            "intermediate_results": {
                "dense_top10": [],
                "sparse_top10": [],
                "rrf_top10": [],
                "rerank_top10": [],
                "agent_top10": [],
            },
            "final_results": [],
            "performance_metrics": {
                "total_time": 0,
                "step_times": {},
            },
            "config": {
                "optimization_config": self.optimization_config,
            },
        }

    async def _generate_query_embeddings(
        self,
        query: str,
        is_cpu_explicit: bool,
        log_data: dict[str, Any],
    ) -> tuple[DenseVector, SparseVector | None]:
        """生成查询向量并记录耗时。"""

        t1 = time.perf_counter()
        matcher = self._require_semantic_matcher()
        query_embeddings = await matcher._get_embedding(
            query,
            is_query=True,
            return_dense=True,
            return_sparse=not is_cpu_explicit,
        )
        query_dense = query_embeddings["dense_vecs"][0]
        query_sparse = (
            query_embeddings["lexical_weights"][0]
            if not is_cpu_explicit and query_embeddings.get("lexical_weights")
            else None
        )
        t2 = time.perf_counter()
        log_data["performance_metrics"]["step_times"]["step1_embedding"] = (t2 - t1) * 1000
        logger.info("步骤1耗时", duration_ms=(t2 - t1) * 1000)
        return query_dense, query_sparse

    async def _retrieve_candidates(
        self,
        query_dense: DenseVector,
        query_sparse: SparseVector | None,
        filters: DiscoveryFilters | None,
        is_cpu_explicit: bool,
        log_data: dict[str, Any],
    ) -> tuple[list[SkillResult], list[SkillResult]]:
        """执行稠密/稀疏检索并记录中间结果。"""

        k = self.optimization_config["coarse_k"]
        t3 = time.perf_counter()

        async def run_dense() -> list[SkillResult]:
            async with get_async_session_context() as session:
                return await self._dense_retrieval(session, query_dense, k, filters)

        async def run_sparse() -> list[SkillResult]:
            if query_sparse is None:
                return []
            async with get_async_session_context() as session:
                return await self._sparse_retrieval(session, query_sparse, k, filters)

        if is_cpu_explicit:
            dense_results = await run_dense()
            sparse_results: list[SkillResult] = []
        else:
            dense_results, sparse_results = await asyncio.gather(run_dense(), run_sparse())

        t4 = time.perf_counter()
        log_data["performance_metrics"]["step_times"]["step2_retrieval"] = (t4 - t3) * 1000
        log_data["process_info"]["dense_retrieval_count"] = len(dense_results)
        log_data["process_info"]["sparse_retrieval_count"] = len(sparse_results)
        log_data["intermediate_results"]["dense_top10"] = [
            {
                "aic": result["aic"],
                "skill_id": result["skill_id"],
                "score": result["score"],
                "rank": result["rank"],
                "description": result["description"][:100],
            }
            for result in dense_results[:30]
        ]
        log_data["intermediate_results"]["sparse_top10"] = [
            {
                "aic": result["aic"],
                "skill_id": result["skill_id"],
                "score": result["score"],
                "rank": result["rank"],
                "description": result["description"][:100],
            }
            for result in sparse_results[:30]
        ]

        logger.info(
            "步骤2耗时",
            duration_ms=(t4 - t3) * 1000,
            mode="cpu_dense" if is_cpu_explicit else "dense_sparse_hybrid",
        )
        return dense_results, sparse_results

    async def _rank_candidates(
        self,
        query: str,
        dense_results: list[dict[str, Any]],
        sparse_results: list[dict[str, Any]],
        is_cpu_explicit: bool,
        log_data: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        """执行融合、限流和可选 rerank。"""

        t5 = time.perf_counter()
        if is_cpu_explicit:
            final_results = self._rank_dense_for_cpu(query, dense_results)
        else:
            coarse_k = self.optimization_config["coarse_k"]
            final_results = self._hybrid_fusion(dense_results, sparse_results, coarse_k)
        t6 = time.perf_counter()

        if is_cpu_explicit:
            log_data["performance_metrics"]["step_times"]["step3_cpu_ranking"] = (t6 - t5) * 1000
            log_data["process_info"]["cpu_ranking_count"] = len(final_results)
        else:
            log_data["performance_metrics"]["step_times"]["step3_rrf_fusion"] = (t6 - t5) * 1000
            log_data["process_info"]["rrf_fusion_count"] = len(final_results)

        log_data["intermediate_results"]["rrf_top10"] = [
            {
                "aic": result["aic"],
                "skill_id": result["skill_id"],
                "rrf_score": result.get("rrf_score", 0),
                "description": result["description"][:100],
            }
            for result in final_results[:30]
        ]
        logger.info("步骤3耗时", duration_ms=(t6 - t5) * 1000, mode="cpu_ranking" if is_cpu_explicit else "rrf_fusion")

        if self.optimization_config["limit_skills_per_agent"]:
            final_results = self._limit_ranked_skills(final_results, log_data)

        rerank_top_k = 30
        if is_cpu_explicit:
            logger.info("步骤4跳过", reason="cpu_explicit_does_not_use_reranker")
            return final_results, rerank_top_k

        matcher = self._require_semantic_matcher()
        if not matcher.reranker_url:
            logger.info("步骤4跳过", reason="reranker_url_missing")
            return final_results, rerank_top_k

        t7 = time.perf_counter()
        top_results = final_results[:rerank_top_k]
        remaining_results = final_results[rerank_top_k:]
        reranked_results = await matcher.rerank_results(query, top_results)
        final_results = reranked_results + remaining_results

        t8 = time.perf_counter()
        log_data["performance_metrics"]["step_times"]["step4_rerank"] = (t8 - t7) * 1000
        log_data["process_info"]["rerank_count"] = len(reranked_results)
        log_data["intermediate_results"]["rerank_top10"] = [
            {
                "aic": result["aic"],
                "skill_id": result["skill_id"],
                "rerank_score": result.get("rerank_score", 0),
                "rrf_score": result.get("rrf_score", 0),
                "description": result["description"][:100],
            }
            for result in reranked_results[:30]
        ]
        logger.info("步骤4耗时", duration_ms=(t8 - t7) * 1000, rerank_top_k=rerank_top_k)
        return final_results, rerank_top_k

    def _limit_ranked_skills(
        self,
        final_results: list[dict[str, Any]],
        log_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """按智能体限制候选技能数量并记录耗时。"""

        t_limit_start = time.perf_counter()
        max_skills = self.optimization_config["max_skills_per_agent"]
        original_count = len(final_results)
        limited_results = self._limit_skills_per_agent(final_results, max_skills)
        t_limit_end = time.perf_counter()

        log_data["performance_metrics"]["step_times"]["step3.5_limit_skills"] = (t_limit_end - t_limit_start) * 1000
        log_data["process_info"]["skill_limit_applied"] = True
        log_data["process_info"]["skills_before_limit"] = original_count
        log_data["process_info"]["skills_after_limit"] = len(limited_results)
        logger.info(
            "步骤3.5耗时",
            duration_ms=(t_limit_end - t_limit_start) * 1000,
            original_count=original_count,
            limited_count=len(limited_results),
        )
        return limited_results

    def _filter_relevant_skills(
        self,
        final_results: list[dict[str, Any]],
        is_cpu_explicit: bool,
        rerank_top_k: int,
        log_data: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], float]:
        """按相关性阈值筛选最终候选。"""

        t9 = time.perf_counter()
        relevance_threshold = self.optimization_config["relevance_threshold"]

        if is_cpu_explicit:
            filtered_skills = final_results
        else:
            filtered_skills = self._truncate_by_rerank_threshold(final_results, rerank_top_k, relevance_threshold)

        t10 = time.perf_counter()
        log_data["performance_metrics"]["step_times"]["step5_reconstruct"] = (t10 - t9) * 1000
        log_data["intermediate_results"]["skill_top10"] = [
            {
                "aic": skill.get("aic", ""),
                "skill_id": skill.get("skill_id", ""),
                "rerank_score": skill.get("rerank_score"),
                "rrf_score": skill.get("rrf_score"),
                "description": skill.get("description", "")[:100],
            }
            for skill in filtered_skills[:10]
        ]

        if is_cpu_explicit:
            logger.info("步骤5耗时", duration_ms=(t10 - t9) * 1000, mode="cpu_passthrough")
            logger.info("CPU 排序后剩余技能数", count=len(filtered_skills))
        else:
            logger.info("步骤5耗时", duration_ms=(t10 - t9) * 1000, mode="threshold_filter")
            logger.info("过滤后剩余技能数", count=len(filtered_skills), relevance_threshold=relevance_threshold)

        return filtered_skills, relevance_threshold

    def _truncate_by_rerank_threshold(
        self,
        final_results: list[dict[str, Any]],
        rerank_top_k: int,
        relevance_threshold: float,
    ) -> list[dict[str, Any]]:
        """根据 rerank 分数阈值截断前段结果。"""

        filtered_skills: list[dict[str, Any]] = []
        for skill in final_results[:rerank_top_k]:
            rerank_score = skill.get("rerank_score")
            if rerank_score is not None and rerank_score < relevance_threshold:
                return filtered_skills
            filtered_skills.append(skill)

        return filtered_skills + final_results[rerank_top_k:]

    async def _apply_cpu_llm_final_selection(
        self,
        query: str,
        filtered_skills: list[dict[str, Any]],
        limit: int,
        log_data: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        """CPU explicit 模式下调用 LLM 做最终技能终选。"""

        llm_reasoning = ""
        t_cpu_llm_start = time.perf_counter()
        llm_applied = False
        llm_returned_count = 0

        cpu_candidate_k = self.optimization_config["cpu_llm_candidate_k"]
        llm_top_n = limit if limit and limit > 0 else 5
        llm_candidate_count = min(cpu_candidate_k, llm_top_n)
        llm_candidates = filtered_skills[:llm_candidate_count]

        try:
            llm_result = await self._llm_rerank_cpu_skills(query, llm_candidates, llm_top_n)
            llm_ranked_skills = llm_result.get("skills", [])
            llm_reasoning = llm_result.get("reasoning", "")
            llm_returned_count = int(llm_result.get("llm_returned_count", 0) or 0)

            if llm_ranked_skills:
                filtered_skills = llm_ranked_skills
                llm_applied = True
                logger.info("步骤5.5 CPU LLM终选完成", returned_count=len(filtered_skills))
            else:
                logger.info("步骤5.5 CPU LLM未返回有效技能，回退到 CPU 排序结果")
        except (OpenAIError, RuntimeError, ValueError, TypeError) as llm_exc:
            logger.warning("步骤5.5 CPU LLM终选失败，回退到 CPU 排序结果", error=str(llm_exc))

        t_cpu_llm_end = time.perf_counter()
        logger.info(
            "步骤5.5耗时",
            duration_ms=(t_cpu_llm_end - t_cpu_llm_start) * 1000,
            llm_applied=llm_applied,
            candidates_sent=len(llm_candidates),
            returned_count=llm_returned_count,
        )
        log_data["performance_metrics"]["step_times"]["step5.5_cpu_llm"] = (t_cpu_llm_end - t_cpu_llm_start) * 1000
        log_data["process_info"]["cpu_llm_applied"] = llm_applied
        log_data["process_info"]["cpu_llm_candidates_sent"] = len(llm_candidates)
        log_data["process_info"]["cpu_llm_returned_count"] = llm_returned_count

        if llm_reasoning:
            log_data["process_info"]["cpu_llm_reasoning"] = llm_reasoning

        log_data["intermediate_results"]["cpu_llm_top10"] = [
            {
                "aic": skill.get("aic", ""),
                "skill_id": skill.get("skill_id", ""),
                "cpu_llm_score": skill.get("cpu_llm_score"),
                "cpu_llm_reason": (skill.get("cpu_llm_reason", "") or "")[:120],
            }
            for skill in filtered_skills[:10]
        ]
        return filtered_skills, llm_reasoning

    def _format_discovery_results(
        self,
        filtered_skills: list[dict[str, Any]],
        limit: int,
        log_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """将技能候选格式化为最终响应结构。"""

        t11 = time.perf_counter()
        result: list[dict[str, Any]] = []

        for ranking, skill in enumerate(filtered_skills, 1):
            result.append(
                {
                    "acs": skill.get("acs", {}),
                    "skillId": skill.get("skill_id", ""),
                    "ranking": ranking,
                    "memo": self._build_result_memo(skill),
                }
            )
            if limit and len(result) >= limit:
                break

        t12 = time.perf_counter()
        log_data["performance_metrics"]["step_times"]["step6_format"] = (t12 - t11) * 1000
        log_data["process_info"]["final_returned_count"] = len(result)
        log_data["final_results"] = [
            {
                "aic": item["acs"].get("aic", ""),
                "agent_name": item["acs"].get("name", ""),
                "skillId": item["skillId"],
                "ranking": item["ranking"],
                "memo": item["memo"],
            }
            for item in result
        ]
        logger.info("步骤6耗时", duration_ms=(t12 - t11) * 1000, mode="format_output")
        return result

    def _build_result_memo(self, skill: dict[str, Any]) -> str:
        """根据技能排序来源构建 memo。"""

        memo_parts: list[str] = []
        if "cpu_llm_score" in skill:
            memo_parts.append(f"LLM分数: {skill.get('cpu_llm_score', 0.0):.4f}")
            if skill.get("cpu_llm_reason"):
                memo_parts.append(f"LLM理由: {skill.get('cpu_llm_reason', '')[:80]}")
        elif "rerank_score" in skill and skill["rerank_score"] is not None:
            memo_parts.append(f"Rerank分数: {skill['rerank_score']:.4f}")
        elif "rrf_score" in skill and skill["rrf_score"] is not None:
            memo_parts.append(f"RRF分数: {skill['rrf_score']:.4f}")
        return " | ".join(memo_parts)

    def _finalize_discovery_success(
        self,
        log_data: dict[str, Any],
        result: list[dict[str, Any]],
        total_start: float,
        is_cpu_explicit: bool,
        relevance_threshold: float,
    ) -> None:
        """记录成功路径的总耗时和最终日志。"""

        total_end = time.perf_counter()
        log_data["performance_metrics"]["total_time"] = (total_end - total_start) * 1000
        logger.info("总耗时", duration_ms=(total_end - total_start) * 1000)
        if is_cpu_explicit:
            logger.info("最终返回技能结果", count=len(result), mode="cpu_explicit")
        else:
            logger.info(
                "最终返回技能结果",
                count=len(result),
                relevance_threshold=relevance_threshold,
                mode="threshold_filter",
            )
        self._save_discovery_log(log_data)

    def _record_discovery_failure(
        self,
        error: Exception,
        log_data: dict[str, Any] | None,
        total_start: float,
    ) -> None:
        """记录发现流程失败日志。"""

        logger.exception("混合检索失败", error=str(error))
        if log_data is None:
            return

        import traceback

        log_data["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
        log_data["performance_metrics"]["total_time"] = (time.perf_counter() - total_start) * 1000
        self._save_discovery_log(log_data)

    async def _dense_retrieval(
        self,
        session: AsyncSession,
        query_vector: DenseVector,
        k: int,
        filters: DiscoveryFilters | None = None,
    ) -> list[SkillResult]:
        """
        稠密向量检索 (使用 pgvector 的余弦相似度)
        支持动态过滤条件
        """
        # 基础参数
        params = {"query_vector": query_vector.tolist(), "k": k}

        # 获取过滤条件
        filter_clauses, filter_params = self._build_filter_clauses(filters, table_alias="a")
        params.update(filter_params)

        # 拼接 WHERE 子句
        where_clause = ""
        if filter_clauses:
            where_clause = "WHERE " + SQL_AND.join(filter_clauses)

        # 构建完整 SQL
        sql = f"""
            SELECT
                s.id,
                s.aic,
                s.skill_id,
                s.description,
                1 - (s.embedding <=> CAST(:query_vector AS vector)) AS similarity,
                a.acs
            FROM skills s
            LEFT JOIN agents a ON s.aic = a.aic
            {where_clause}
            ORDER BY s.embedding <=> CAST(:query_vector AS vector)
            LIMIT :k
        """  # noqa: S608
        query = text(sql).bindparams(
            bindparam("query_vector", type_=Vector()),
            bindparam("k", type_=Integer()),
        )
        result = await session.execute(query, params)
        rows = result.fetchall()

        results: list[SkillResult] = []
        for rank, row in enumerate(rows, 1):
            results.append(
                {
                    "id": row[0],
                    "aic": row[1],
                    "skill_id": row[2],
                    "description": row[3],
                    "score": float(row[4]),
                    "rank": rank,
                    "method": "dense",
                    "acs": row[5],
                }
            )

        logger.info("稠密检索完成", result_count=len(results), filter_clause_count=len(filter_clauses))
        return results

    async def _sparse_retrieval(
        self,
        session: AsyncSession,
        query_sparse: dict[str, float],
        k: int,
        filters: DiscoveryFilters | None = None,
    ) -> list[SkillResult]:
        """
        稀疏向量检索 (使用GIN索引)
        支持动态过滤条件

        """
        query_sparse_clean = {str(t): float(w) for t, w in query_sparse.items()}

        query_sparse_json = json.dumps(query_sparse_clean, ensure_ascii=False)

        params = {
            "query_sparse": query_sparse_json,
            "k": k,
        }

        # 获取过滤条件
        filter_clauses, filter_params = self._build_filter_clauses(filters, table_alias="a")
        params.update(filter_params)

        additional_where = ""
        if filter_clauses:
            additional_where = SQL_AND + SQL_AND.join(filter_clauses)

        sql = f"""
            WITH query_tokens AS (
                SELECT
                    key AS token,
                    value::float AS weight
                FROM jsonb_each_text(CAST(:query_sparse AS jsonb))
            ),
            scores AS (
                SELECT
                    s.id,
                    s.aic,
                    s.skill_id,
                    s.description,
                    SUM(
                        LEAST(
                            qt.weight,
                            (s.sparse_embedding -> qt.token)::text::float
                        )
                    ) AS similarity
                FROM skills s
                LEFT JOIN agents a ON s.aic = a.aic
                CROSS JOIN query_tokens qt
                WHERE s.sparse_embedding ? qt.token
                {additional_where}
                GROUP BY s.id, s.aic, s.skill_id, s.description
            )
            SELECT
                sc.id,
                sc.aic,
                sc.skill_id,
                sc.description,
                sc.similarity,
                a.acs
            FROM scores sc
            LEFT JOIN agents a ON sc.aic = a.aic
            ORDER BY sc.similarity DESC
            LIMIT :k
        """  # noqa: S608

        query = text(sql).bindparams(
            bindparam("query_sparse", type_=Text()),  # 关键：按“文本”传入
            bindparam("k", type_=Integer()),
        )

        result = await session.execute(query, params)
        rows = result.fetchall()

        results: list[SkillResult] = []
        for rank, row in enumerate(rows, 1):
            results.append(
                {
                    "id": row[0],
                    "aic": row[1],
                    "skill_id": row[2],
                    "description": row[3],
                    "score": float(row[4]) if row[4] else 0.0,
                    "rank": rank,
                    "method": "sparse",
                    "acs": row[5],
                }
            )

        logger.info("稀疏检索完成", result_count=len(results), filter_clause_count=len(filter_clauses))
        return results

    def _hybrid_fusion(
        self, dense_results: list[SkillResult], sparse_results: list[SkillResult], n: int
    ) -> list[SkillResult]:
        """
        混合融合 - 使用RRF (Reciprocal Rank Fusion)

        Args:
            dense_results: 稠密检索结果
            sparse_results: 稀疏检索结果
            n: 最终返回数量

        Returns:
            融合后的top-n结果
        """
        k = 60  # RRF参数,一般取60

        # 计算RRF分数
        rrf_scores: dict[tuple[str, str], float] = {}

        # 处理稠密检索结果
        for item in dense_results:
            key = (item["aic"], item["skill_id"])
            rank = item["rank"]
            rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (k + rank)

        # 处理稀疏检索结果
        for item in sparse_results:
            key = (item["aic"], item["skill_id"])
            rank = item["rank"]
            rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (k + rank)

        # 排序并取top-n
        sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:n]

        # 构建最终结果,保留完整信息
        final_results: list[dict[str, Any]] = []
        for (aic, skill_id), score in sorted_items:
            # 从原始结果中找到对应的完整信息
            merged_item: dict[str, Any] | None = None
            for r in dense_results + sparse_results:
                if r["aic"] == aic and r["skill_id"] == skill_id:
                    merged_item = r.copy()
                    break

            if merged_item:
                merged_item["rrf_score"] = score
                final_results.append(merged_item)

        logger.info("混合融合完成", result_count=len(final_results))

        return final_results

    def _tokenize_for_cpu(self, text: str) -> set[str]:
        """CPU 模式下的轻量分词。"""
        if not text:
            return set()
        tokens = re.findall(r"\w+|[\u4e00-\u9fff]", text.lower())
        return {t for t in tokens if t and t.strip()}

    def _rank_dense_for_cpu(self, query: str, dense_results: list[SkillResult]) -> list[SkillResult]:
        """CPU explicit 排序：稠密分数 + 关键词重叠融合。"""
        if not dense_results:
            return []

        query_tokens = self._tokenize_for_cpu(query)
        ranked_results: list[SkillResult] = []

        for item in dense_results:
            dense_score = float(item.get("score") or 0.0)
            description_tokens = self._tokenize_for_cpu(item.get("description") or "")

            keyword_score = 0.0
            if query_tokens:
                keyword_score = len(query_tokens & description_tokens) / len(query_tokens)

            final_score = dense_score + 0.2 * keyword_score
            merged = item.copy()
            merged["cpu_dense_score"] = dense_score
            merged["cpu_keyword_score"] = keyword_score
            merged["cpu_final_score"] = final_score
            ranked_results.append(merged)

        ranked_results.sort(key=lambda x: x.get("cpu_final_score", 0.0), reverse=True)
        for rank, row in enumerate(ranked_results, 1):
            row["rank"] = rank

        logger.info("CPU explicit 排序完成", candidate_count=len(ranked_results))
        return ranked_results

    def _prepare_cpu_skill_info_for_llm(self, skills_list: list[SkillResult]) -> str:
        """将CPU候选技能压缩为适合LLM评估的JSON。"""
        compact_skills: list[dict[str, Any]] = []
        for i, skill in enumerate(skills_list, 1):
            compact_skills.append(
                {
                    "id": i,
                    "aic": skill.get("aic", ""),
                    "skillid": skill.get("skill_id", ""),
                    "description": skill.get("description", ""),
                }
            )
        return json.dumps(compact_skills, ensure_ascii=False, separators=(",", ":"))

    def _create_cpu_llm_discovery_prompt(self, query: str, skills_list: list[SkillResult], top_n: int) -> str:
        """构造CPU模式LLM终选提示词。"""
        skill_info_json = self._prepare_cpu_skill_info_for_llm(skills_list)
        return f"""
你是一个AI技能匹配专家。请根据用户查询，从候选技能中选出最匹配的 {top_n} 个技能。

[用户查询]
{query}

[候选技能（JSON）]
{skill_info_json}

[要求]
1. 优先看技能描述与查询语义的匹配度。
2. 如果候选整体不匹配，也要给低分。
3. 仅返回最匹配的 {top_n} 个技能。

[输出格式]
严格输出JSON，不要输出代码块标记：
{{
  "reasoning": "20字以内简单说明筛选逻辑",
  "skills": [
    {{
      "aic": "agent-aic",
      "skillid": "skill-id",
      "score": 0.86,
      "reason": "简短理由"
    }}
  ]
}}
""".strip()

    async def _call_llm_for_cpu_discovery(self, prompt: str) -> str:
        """调用大模型对CPU候选结果做终选。"""
        if not self.api_key:
            raise ValueError("API密钥未设置，请配置 DISCOVERY_LLM_API_KEY")
        if not self.base_url:
            raise ValueError("未配置 DISCOVERY_LLM_BASE_URL，无法调用 Discovery LLM")

        if self.client is None:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )

        response = await self.client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.1,
        )

        if not response.choices:
            raise RuntimeError("Discovery LLM 响应格式异常")
        content = response.choices[0].message.content or ""
        if not content:
            raise RuntimeError("Discovery LLM 响应内容为空")
        return content

    def _extract_json_payload(self, response_text: str) -> str:
        """从LLM响应中提取JSON文本。"""
        text = (response_text or "").strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        if text.startswith("{") and text.endswith("}"):
            return text

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1]
        return text

    async def _llm_rerank_cpu_skills(self, query: str, skills_list: list[SkillResult], top_n: int) -> dict[str, Any]:
        """CPU explicit 场景下调用LLM对候选技能做最终排序。"""
        if not skills_list:
            return {"reasoning": "", "skills": [], "llm_returned_count": 0, "candidates_sent": 0}

        prompt = self._create_cpu_llm_discovery_prompt(query, skills_list, top_n)
        response = await self._call_llm_for_cpu_discovery(prompt)
        payload_text = self._extract_json_payload(response)
        payload = json.loads(payload_text)

        llm_reasoning = payload.get("reasoning", "")
        llm_skills = payload.get("skills", [])

        candidate_map = {(s.get("aic", ""), s.get("skill_id", "")): s for s in skills_list}
        used_keys = set()
        reranked_skills: list[SkillResult] = []

        for item in llm_skills:
            aic = str(item.get("aic", "") or "")
            skill_id = str(item.get("skillid", "") or "")
            key = (aic, skill_id)

            if not aic or key in used_keys or key not in candidate_map:
                continue

            merged = candidate_map[key].copy()
            try:
                merged["cpu_llm_score"] = float(item.get("score", 0.0))
            except TypeError, ValueError:
                merged["cpu_llm_score"] = 0.0
            merged["cpu_llm_reason"] = str(item.get("reason", "") or "")
            reranked_skills.append(merged)
            used_keys.add(key)

            if top_n and len(reranked_skills) >= top_n:
                break

        return {
            "reasoning": llm_reasoning,
            "skills": reranked_skills,
            "llm_returned_count": len(llm_skills),
            "candidates_sent": len(skills_list),
        }

    def _reconstruct_agents_from_skills(self, skill_results: list[SkillResult]) -> list[dict[str, Any]]:
        """
        从 skill 结果中聚合 agent 信息（纯内存操作，无需数据库查询）
        """
        from collections import defaultdict

        aic_to_skills = defaultdict(list)

        for skill in skill_results:
            aic_to_skills[skill["aic"]].append(skill)

        agents: list[dict[str, Any]] = []

        for aic, skills in aic_to_skills.items():
            # 对该 agent 的 skills 按分数排序（rerank_score 优先，否则用 rrf_score）
            skills.sort(
                key=lambda skill: float(skill.get("rerank_score") or skill.get("rrf_score") or 0.0),
                reverse=True,
            )

            # 所有同 aic 的 skill 都有相同的 acs，取第一个即可
            acs = skills[0].get("acs", {})

            agent_dict = {
                "aic": aic,
                "acs": acs,  # 完整的 agent 信息
                "matched_skills": skills,
                "best_skill_score": skills[0].get("rerank_score", skills[0].get("rrf_score", 0)),
            }
            agents.append(agent_dict)

        # 按最佳 skill 分数对 agents 排序
        agents.sort(key=lambda a: a.get("best_skill_score", 0), reverse=True)

        logger.info("Agent 重构完成", agent_count=len(agents))

        return agents

    @staticmethod
    def _validate_filter_table_alias(table_alias: str) -> str:
        """校验过滤 SQL 中允许使用的表别名。"""

        if table_alias != "a":
            raise ValueError(f"Unsupported filter table alias: {table_alias}")
        return table_alias

    def _append_default_filter_clauses(
        self,
        filters: DiscoveryFilters,
        table_alias: str,
        filter_clauses: list[str],
        params: dict[str, object],
    ) -> None:
        """追加默认可用性、入口和 active 过滤条件。"""

        if filters.onlyAvailable is not False:
            filter_clauses.append(
                """
                (
                    NOT EXISTS (SELECT 1 FROM available_agents_runtime)
                    OR EXISTS (
                        SELECT 1
                        FROM available_agents_runtime aar
                        WHERE aar.aic = s.aic
                          AND aar.is_available = true
                    )
                )
                """
            )

        if filters.hasEndpoints is None and filters.hasWebAppUrl is None:
            filter_clauses.append(f"""
                (
                    ({table_alias}.acs->'endPoints' IS NOT NULL
                    AND jsonb_typeof({table_alias}.acs->'endPoints') = 'array'
                    AND jsonb_array_length({table_alias}.acs->'endPoints') > 0)
                    OR
                    ({table_alias}.acs->>'webAppUrl' IS NOT NULL
                    AND {table_alias}.acs->>'webAppUrl' != '')
                )
            """)

        if filters.isActive is None:
            filter_clauses.append(f"({table_alias}.acs->>'active')::boolean = true")
        else:
            filter_clauses.append(f"({table_alias}.acs->>'active')::boolean = :is_active")
            params["is_active"] = filters.isActive

        if filters.hasEndpoints is not None:
            if filters.hasEndpoints:
                filter_clauses.append(f"""
                    {table_alias}.acs->'endPoints' IS NOT NULL
                    AND jsonb_typeof({table_alias}.acs->'endPoints') = 'array'
                    AND jsonb_array_length({table_alias}.acs->'endPoints') > 0
                """)
            else:
                filter_clauses.append(f"""
                    ({table_alias}.acs->'endPoints' IS NULL
                    OR jsonb_typeof({table_alias}.acs->'endPoints') != 'array'
                    OR jsonb_array_length({table_alias}.acs->'endPoints') = 0)
                """)

        if filters.hasWebAppUrl is not None:
            if filters.hasWebAppUrl:
                filter_clauses.append(f"""
                    {table_alias}.acs->>'webAppUrl' IS NOT NULL
                    AND {table_alias}.acs->>'webAppUrl' != ''
                """)
            else:
                filter_clauses.append(f"""
                    ({table_alias}.acs->>'webAppUrl' IS NULL
                    OR {table_alias}.acs->>'webAppUrl' = '')
                """)

    def _append_protocol_transport_security_filters(
        self,
        filters: DiscoveryFilters,
        table_alias: str,
        filter_clauses: list[str],
        params: dict[str, object],
    ) -> None:
        """追加协议版本、传输协议和安全方案过滤条件。"""

        if filters.protocolVersions:
            filter_clauses.append(f"{table_alias}.acs->>'protocolVersion' = ANY(:protocol_versions)")
            params["protocol_versions"] = filters.protocolVersions

        if filters.protocolVersions_reject:
            filter_clauses.append(f"{table_alias}.acs->>'protocolVersion' != ALL(:protocol_versions_reject)")
            params["protocol_versions_reject"] = filters.protocolVersions_reject

        if filters.transports:
            filter_clauses.append(f"""
                EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements({table_alias}.acs->'endPoints') AS ep
                    WHERE ep->>'transport' = ANY(:transports)
                )
            """)  # noqa: S608
            params["transports"] = filters.transports

        if filters.transports_reject:
            filter_clauses.append(f"""
                NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements({table_alias}.acs->'endPoints') AS ep
                    WHERE ep->>'transport' = ANY(:transports_reject)
                )
            """)  # noqa: S608
            params["transports_reject"] = filters.transports_reject

        if filters.requiredSecuritySchemes:
            filter_clauses.append(f"""
                EXISTS (
                    SELECT 1
                    FROM jsonb_object_keys({table_alias}.acs->'securitySchemes') AS scheme
                    WHERE scheme = ANY(:required_security_schemes)
                )
            """)  # noqa: S608
            params["required_security_schemes"] = filters.requiredSecuritySchemes

        if filters.requiredSecuritySchemes_reject:
            filter_clauses.append(f"""
                NOT EXISTS (
                    SELECT 1
                    FROM jsonb_object_keys({table_alias}.acs->'securitySchemes') AS scheme
                    WHERE scheme = ANY(:required_security_schemes_reject)
                )
            """)  # noqa: S608
            params["required_security_schemes_reject"] = filters.requiredSecuritySchemes_reject

    def _append_skill_provider_filters(
        self,
        filters: DiscoveryFilters,
        table_alias: str,
        filter_clauses: list[str],
        params: dict[str, object],
    ) -> None:
        """追加技能与提供者相关过滤条件。"""

        if filters.skillTags:
            filter_clauses.append(f"""
                EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements({table_alias}.acs->'skills') AS skill
                    WHERE skill->>'id' = s.skill_id
                    AND EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(skill->'tags') AS tag
                        WHERE tag = ANY(:skill_tags)
                    )
                )
            """)  # noqa: S608
            params["skill_tags"] = filters.skillTags

        if filters.skillTags_reject:
            filter_clauses.append(f"""
                NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements({table_alias}.acs->'skills') AS skill
                    WHERE skill->>'id' = s.skill_id
                    AND EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(skill->'tags') AS tag
                        WHERE tag = ANY(:skill_tags_reject)
                    )
                )
            """)  # noqa: S608
            params["skill_tags_reject"] = filters.skillTags_reject

        if filters.skillIds:
            filter_clauses.append("s.skill_id = ANY(:skill_ids)")
            params["skill_ids"] = filters.skillIds

        if filters.skillIds_reject:
            filter_clauses.append("s.skill_id != ALL(:skill_ids_reject)")
            params["skill_ids_reject"] = filters.skillIds_reject

        if filters.providerCountryCodes:
            filter_clauses.append(f"""
                {table_alias}.acs->'provider'->>'countryCode' = ANY(:provider_country_codes)
            """)
            params["provider_country_codes"] = filters.providerCountryCodes

        if filters.providerCountryCodes_reject:
            filter_clauses.append(f"""
                {table_alias}.acs->'provider'->>'countryCode' != ALL(:provider_country_codes_reject)
            """)
            params["provider_country_codes_reject"] = filters.providerCountryCodes_reject

        if filters.providerOrganizations:
            filter_clauses.append(f"""
                {table_alias}.acs->'provider'->>'organization' = ANY(:provider_organizations)
            """)
            params["provider_organizations"] = filters.providerOrganizations

        if filters.providerOrganizations_reject:
            filter_clauses.append(f"""
                {table_alias}.acs->'provider'->>'organization' != ALL(:provider_organizations_reject)
            """)
            params["provider_organizations_reject"] = filters.providerOrganizations_reject

        if filters.providerLicenses:
            filter_clauses.append(f"""
                {table_alias}.acs->'provider'->>'license' = ANY(:provider_licenses)
            """)
            params["provider_licenses"] = filters.providerLicenses

        if filters.providerLicenses_reject:
            filter_clauses.append(f"""
                {table_alias}.acs->'provider'->>'license' != ALL(:provider_licenses_reject)
            """)
            params["provider_licenses_reject"] = filters.providerLicenses_reject

    def _append_mode_filters(
        self,
        filters: DiscoveryFilters,
        table_alias: str,
        filter_clauses: list[str],
        params: dict[str, object],
    ) -> None:
        """追加输入输出模式过滤条件。"""

        if filters.inputModes:
            filter_clauses.append(f"""
                (
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text({table_alias}.acs->'defaultInputModes') AS mode
                        WHERE mode = ANY(:input_modes)
                    )
                    OR
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements({table_alias}.acs->'skills') AS skill
                        WHERE skill->>'id' = s.skill_id
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements_text(skill->'inputModes') AS mode
                            WHERE mode = ANY(:input_modes)
                        )
                    )
                )
            """)  # noqa: S608
            params["input_modes"] = filters.inputModes

        if filters.inputModes_reject:
            filter_clauses.append(f"""
                NOT (
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text({table_alias}.acs->'defaultInputModes') AS mode
                        WHERE mode = ANY(:input_modes_reject)
                    )
                    OR
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements({table_alias}.acs->'skills') AS skill
                        WHERE skill->>'id' = s.skill_id
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements_text(skill->'inputModes') AS mode
                            WHERE mode = ANY(:input_modes_reject)
                        )
                    )
                )
            """)  # noqa: S608
            params["input_modes_reject"] = filters.inputModes_reject

        if filters.outputModes:
            filter_clauses.append(f"""
                (
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text({table_alias}.acs->'defaultOutputModes') AS mode
                        WHERE mode = ANY(:output_modes)
                    )
                    OR
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements({table_alias}.acs->'skills') AS skill
                        WHERE skill->>'id' = s.skill_id
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements_text(skill->'outputModes') AS mode
                            WHERE mode = ANY(:output_modes)
                        )
                    )
                )
            """)  # noqa: S608
            params["output_modes"] = filters.outputModes

        if filters.outputModes_reject:
            filter_clauses.append(f"""
                NOT (
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text({table_alias}.acs->'defaultOutputModes') AS mode
                        WHERE mode = ANY(:output_modes_reject)
                    )
                    OR
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements({table_alias}.acs->'skills') AS skill
                        WHERE skill->>'id' = s.skill_id
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements_text(skill->'outputModes') AS mode
                            WHERE mode = ANY(:output_modes_reject)
                        )
                    )
                )
            """)  # noqa: S608
            params["output_modes_reject"] = filters.outputModes_reject

    def _append_capability_identity_filters(
        self,
        filters: DiscoveryFilters,
        table_alias: str,
        filter_clauses: list[str],
        params: dict[str, object],
    ) -> None:
        """追加能力和身份相关过滤条件。"""

        if filters.capabilities is not None:
            cap = filters.capabilities

            if cap.streaming is not None:
                filter_clauses.append(f"""
                    ({table_alias}.acs->'capabilities'->>'streaming')::boolean = :streaming
                """)
                params["streaming"] = cap.streaming

            if cap.notification is not None:
                filter_clauses.append(f"""
                    ({table_alias}.acs->'capabilities'->>'notification')::boolean = :notification
                """)
                params["notification"] = cap.notification

            if cap.messageQueue:
                filter_clauses.append(f"""
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text({table_alias}.acs->'capabilities'->'messageQueue') AS mq
                        WHERE mq = ANY(:message_queue)
                    )
                """)  # noqa: S608
                params["message_queue"] = cap.messageQueue

            if cap.messageQueue_reject:
                filter_clauses.append(f"""
                    NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text({table_alias}.acs->'capabilities'->'messageQueue') AS mq
                        WHERE mq = ANY(:message_queue_reject)
                    )
                """)  # noqa: S608
                params["message_queue_reject"] = cap.messageQueue_reject

        if filters.aic is not None:
            filter_clauses.append("s.aic = :aic")
            params["aic"] = filters.aic

        if filters.aicStartWith is not None:
            filter_clauses.append("s.aic LIKE :aic_prefix")
            params["aic_prefix"] = f"{filters.aicStartWith}%"

        if filters.entityUserId is not None:
            filter_clauses.append(f"{table_alias}.acs->>'entityUserId' = :entity_user_id")
            params["entity_user_id"] = filters.entityUserId

    def _build_filter_clauses(
        self, filters: DiscoveryFilters | None, table_alias: str = "a"
    ) -> tuple[list[str], dict[str, object]]:
        """
        构建过滤条件的 SQL 子句和参数
        支持必要(require)和排除(reject)两种模式

        默认条件：hasEndpoints 和 hasWebAppUrl 都不设置时，要求至少提供其中一个，
        当 filters 为 None 或者 isActive 未设置时，默认只查询 active 的。

        过滤规则：
        - Boolean 类型: None=不筛选, True/False=按值筛选
        - List 类型: None或[]=不筛选, 有值=筛选
        - String/Int 类型: None=不筛选, 有值=筛选
        """
        filter_clauses: list[str] = []
        params: dict[str, object] = {}
        filters = filters or DiscoveryFilters()
        table_alias = self._validate_filter_table_alias(table_alias)

        self._append_default_filter_clauses(filters, table_alias, filter_clauses, params)
        self._append_protocol_transport_security_filters(filters, table_alias, filter_clauses, params)
        self._append_skill_provider_filters(filters, table_alias, filter_clauses, params)
        self._append_mode_filters(filters, table_alias, filter_clauses, params)
        self._append_capability_identity_filters(filters, table_alias, filter_clauses, params)

        return filter_clauses, params

    async def _filter_only_query(self, filters: DiscoveryFilters, limit: int = 50) -> list[SkillResult]:
        """
        过滤查询
        用于 type='filtered' 的场景

        Args:
            filters: 过滤条件
            limit: 返回数量限制

        Returns:
            符合过滤条件的技能列表
        """
        try:
            total_start = time.perf_counter()

            # 获取过滤条件
            filter_clauses, params = self._build_filter_clauses(filters, table_alias="a")
            params["limit"] = limit

            where_clause = "WHERE " + " AND ".join(filter_clauses)

            # 构建 SQL - 直接从 skills 表查询并 JOIN agents
            sql = f"""
                SELECT
                    s.id,
                    s.aic,
                    s.skill_id,
                    s.description,
                    a.acs
                FROM skills s
                LEFT JOIN agents a ON s.aic = a.aic
                {where_clause}
                ORDER BY s.aic, s.skill_id
                LIMIT :limit
            """  # noqa: S608

            async with get_async_session_context() as session:
                query = text(sql)
                result = await session.execute(query, params)
                rows = result.fetchall()

            results = []
            for rank, row in enumerate(rows, 1):
                results.append(
                    {
                        "id": row[0],
                        "aic": row[1],
                        "skill_id": row[2],
                        "description": row[3],
                        "score": 1.0,  # 纯过滤没有相关性分数
                        "rank": rank,
                        "method": "filter_only",
                        "acs": row[4],
                    }
                )

            total_time = (time.perf_counter() - total_start) * 1000
            logger.info("纯过滤查询完成", result_count=len(results), duration_ms=total_time)

            return results

        except SQLAlchemyError as e:
            logger.exception("纯过滤查询失败", error=str(e))
            raise
