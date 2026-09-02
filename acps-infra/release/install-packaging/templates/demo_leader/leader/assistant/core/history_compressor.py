"""
Leader Agent Platform - LLM-7 历史压缩器 (HistoryCompressor)

本模块实现 LLM-7 历史压缩功能：
- 输入：当前 historySummary + 待压缩的 recentTurns
- 输出：新的 historySummary 字符串

触发条件：recentTurns 达到阈值（如 6 轮）时压缩最旧的几轮

设计原则：
1. 异步执行，不阻塞主流程
2. 使用 session 级锁避免竞态
3. 保留最近 N 轮原文，压缩更早的历史
"""

import asyncio
import logging

from ..llm.client import LLMClient, get_llm_client
from ..models.base import now_iso
from ..models.history_compression import (
    COMPRESSION_THRESHOLD,
    MAX_SUMMARY_LENGTH,
    CompressionTurn,
    HistoryCompressionRequest,
    HistoryCompressionResult,
)
from ..services.scenario_loader import ScenarioLoader, get_scenario_loader

logger = logging.getLogger(__name__)


class HistoryCompressor:
 """
    LLM-7 历史压缩器。

    将较早的对话历史压缩为摘要，保留最近几轮原文用于指代消解。

    核心职责：
    1. 判断是否需要压缩（基于 turns 数量阈值）
    2. 调用 LLM 生成压缩摘要
    3. 合并新旧摘要
 """

    def __init__(
        self,
        llm_client: LLMClient,
        scenario_loader: ScenarioLoader,
    ):
 """
        初始化历史压缩器。

        Args:
            llm_client: LLM 客户端
            scenario_loader: 场景加载器
 """
        self._llm_client = llm_client
        self._scenario_loader = scenario_loader

    def should_compress(
        self,
        turns_count: int,
        threshold: int = COMPRESSION_THRESHOLD,
    ) -> bool:
 """
        判断是否需要执行历史压缩。

        Args:
            turns_count: 当前对话轮数
            threshold: 压缩阈值

        Returns:
            是否需要压缩
 """
        return turns_count >= threshold

    async def compress(
        self,
        request: HistoryCompressionRequest,
    ) -> HistoryCompressionResult:
 """
        执行历史压缩。

        将待压缩的轮次生成摘要，与现有摘要合并。

        Args:
            request: 压缩请求

        Returns:
            HistoryCompressionResult
 """
        turns_to_compress = request.turns_to_compress
        existing_summary = request.existing_summary
        existing_turn_count = request.existing_turn_count
        scenario_id = request.scenario_id

        if not turns_to_compress:
            return HistoryCompressionResult(
                new_summary=existing_summary or "",
                compressed_turn_count=0,
                total_turn_count=existing_turn_count,
                compression_timestamp=now_iso,
            )

 # 构建 LLM prompt
        prompt = self._build_compression_prompt(
            turns_to_compress=turns_to_compress,
            existing_summary=existing_summary,
            scenario_id=scenario_id,
        )

 # 获取系统提示词
        system_prompt = self._get_system_prompt(scenario_id)

 # 调用 LLM
        llm_profile = self._scenario_loader.get_llm_profile("history_compression", scenario_id)
        logger.debug(f"[LLM-7] Using profile: {llm_profile}")
        try:
            llm_response = await asyncio.to_thread(
                self._llm_client.call,
                profile_name=llm_profile,
                system_prompt=system_prompt,
                user_message=prompt,
                temperature=0.3,  # 历史压缩/摘要，需要准确忠实于原文
            )
            new_summary = self._parse_response(llm_response)
        except Exception as e:
            logger.error(f"LLM-7 compression failed: {e}")
 # 降级：使用简单的拼接方式
            new_summary = self._fallback_compression(
                turns_to_compress=turns_to_compress,
                existing_summary=existing_summary,
            )

 # 如果摘要过长，尝试进一步压缩
        if len(new_summary) > MAX_SUMMARY_LENGTH:
            logger.warning(f"Summary too long ({len(new_summary)} chars), truncating...")
            new_summary = new_summary[:MAX_SUMMARY_LENGTH] + "..."

        compressed_count = len(turns_to_compress)
        total_count = existing_turn_count + compressed_count

        return HistoryCompressionResult(
            new_summary=new_summary,
            compressed_turn_count=compressed_count,
            total_turn_count=total_count,
            compression_timestamp=now_iso,
        )

    def _build_compression_prompt(
        self,
        turns_to_compress: list[CompressionTurn],
        existing_summary: str | None,
        scenario_id: str | None,
    ) -> str:
 """
        构建压缩提示词。

        Args:
            turns_to_compress: 待压缩的轮次
            existing_summary: 现有摘要
            scenario_id: 场景 ID

        Returns:
            LLM 提示词
 """
        parts = []

 # 1. 现有摘要（如果有）
        if existing_summary:
            parts.append(f"【已有历史摘要】\n{existing_summary}\n")

 # 2. 待压缩的对话
        parts.append("【需要压缩的新对话】")
        for turn in turns_to_compress:
            role_label = "用户" if turn.role == "user" else "助手"
            intent_info = f" [{turn.intent}]" if turn.intent else ""
            parts.append(f"{role_label}{intent_info}: {turn.content}")

 # 3. 任务说明
        parts.append("\n【任务】")
        parts.append("请将上述对话内容压缩为简洁的摘要，要求：")
        parts.append("1. 保留关键事实、用户偏好、已确认的约束条件")
        parts.append("2. 保留重要的决策结论和任务进展")
        parts.append("3. 使用第三人称客观叙述")
        parts.append("4. 控制在 500 字以内")
        parts.append("5. 如果有已有历史摘要，需要与新对话内容整合")

        return "\n".join(parts)

    def _get_system_prompt(self, scenario_id: str | None) -> str:
 """
        获取系统提示词。

        Args:
            scenario_id: 场景 ID

        Returns:
            系统提示词
 """
        try:
 # 尝试从场景配置加载
            if scenario_id:
                prompts = self._scenario_loader.get_prompts(scenario_id)
                if prompts and "history_compression" in prompts:
                    return prompts["history_compression"].get("system", self._default_system_prompt)

 # 尝试从 base 场景加载
            base_prompts = self._scenario_loader.get_prompts("base")
            if base_prompts and "history_compression" in base_prompts:
                return base_prompts["history_compression"].get("system", self._default_system_prompt)
        except Exception as e:
            logger.warning(f"Failed to load compression prompt: {e}")

        return self._default_system_prompt

    def _default_system_prompt(self) -> str:
 """返回默认的系统提示词。"""
 return """你是一个对话历史压缩专家。你的任务是将对话历史压缩为简洁的摘要。

压缩原则：
1. 保留关键信息：用户需求、偏好、约束条件、已确认的事实
2. 保留任务进展：哪些维度已完成、哪些信息已收集
3. 删除冗余内容：重复的问候、无实质内容的确认、过渡性对话
4. 使用客观叙述：第三人称，简洁明了

输出格式：
直接输出压缩后的摘要文本，不需要其他格式或标记。"""

    def _parse_response(self, response: str) -> str:
 """
        解析 LLM 响应。

        Args:
            response: LLM 响应文本

        Returns:
            压缩后的摘要
 """
 # LLM 直接返回摘要文本，进行基本清理
        summary = response.strip

 # 移除可能的引号包裹
        if summary.startswith('"') and summary.endswith('"'):
            summary = summary[1:-1]
        if summary.startswith("'") and summary.endswith("'"):
            summary = summary[1:-1]

        return summary

    def _fallback_compression(
        self,
        turns_to_compress: list[CompressionTurn],
        existing_summary: str | None,
    ) -> str:
 """
        降级压缩：当 LLM 调用失败时使用简单拼接。

        Args:
            turns_to_compress: 待压缩的轮次
            existing_summary: 现有摘要

        Returns:
            简单拼接的摘要
 """
        parts = []

        if existing_summary:
            parts.append(existing_summary)

 # 简单提取用户输入和关键响应
        for turn in turns_to_compress:
            if turn.role == "user":
                intent_info = f"[{turn.intent}]" if turn.intent else ""
 # 截断过长的内容
                content = turn.content[:100] + "..." if len(turn.content) > 100 else turn.content
                parts.append(f"用户{intent_info}: {content}")

        return " | ".join(parts)


# =============================================================================
# 单例工厂
# =============================================================================

_history_compressor: HistoryCompressor | None = None
_lock = asyncio.Lock


def get_history_compressor(
    llm_client: LLMClient | None = None,
    scenario_loader: ScenarioLoader | None = None,
) -> HistoryCompressor:
 """
    获取 HistoryCompressor 单例。

    Args:
        llm_client: LLM 客户端（可选）
        scenario_loader: 场景加载器（可选）

    Returns:
        HistoryCompressor 实例
 """
    global _history_compressor

    if _history_compressor is None:
        if llm_client is None:
            llm_client = get_llm_client
        if scenario_loader is None:
            scenario_loader = get_scenario_loader

        _history_compressor = HistoryCompressor(
            llm_client=llm_client,
            scenario_loader=scenario_loader,
        )

    return _history_compressor


def reset_history_compressor -> None:
 """重置单例（用于测试）。"""
    global _history_compressor
    _history_compressor = None
