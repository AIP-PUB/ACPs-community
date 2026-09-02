"""
Leader Agent Platform - LLM-7 历史压缩数据模型

本模块定义历史压缩（LLM-7）的输入输出数据模型。
"""

from pydantic import BaseModel, ConfigDict, Field

from .base import IsoDateTimeString


class CompressionTurn(BaseModel):
 """
    待压缩的对话轮次。
 """

    role: str = Field(..., description="角色：user 或 assistant")
    content: str = Field(..., description="消息内容")
    intent: str | None = Field(default=None, description="意图")
    timestamp: str | None = Field(default=None, description="时间戳")


class HistoryCompressionRequest(BaseModel):
 """
    历史压缩请求（LLM-7 输入）。
 """

    session_id: str = Field(
        ...,
        alias="sessionId",
        description="Session ID",
    )
    scenario_id: str | None = Field(
        default=None,
        alias="scenarioId",
        description="当前场景 ID",
    )
    existing_summary: str | None = Field(
        default=None,
        alias="existingSummary",
        description="现有的历史摘要（若存在）",
    )
    existing_turn_count: int = Field(
        default=0,
        alias="existingTurnCount",
        description="现有摘要覆盖的轮数",
    )
    turns_to_compress: list[CompressionTurn] = Field(
        ...,
        alias="turnsToCompress",
        description="需要压缩的对话轮次列表",
    )
    turns_to_keep: int = Field(
        default=3,
        alias="turnsToKeep",
        description="保留的最近轮数",
    )

    model_config = ConfigDict(populate_by_name=True)


class HistoryCompressionResult(BaseModel):
 """
    历史压缩结果（LLM-7 输出）。
 """

    new_summary: str = Field(
        ...,
        alias="newSummary",
        description="更新后的历史摘要",
    )
    compressed_turn_count: int = Field(
        ...,
        alias="compressedTurnCount",
        description="本次压缩的轮数",
    )
    total_turn_count: int = Field(
        ...,
        alias="totalTurnCount",
        description="总计覆盖的轮数",
    )
    compression_timestamp: IsoDateTimeString = Field(
        ...,
        alias="compressionTimestamp",
        description="压缩执行时间",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 压缩配置常量
# =============================================================================

# 触发压缩的阈值：当 turns 数量达到此值时触发压缩
COMPRESSION_THRESHOLD = 6

# 每次压缩保留的最近轮数
TURNS_TO_KEEP = 3

# 最大摘要长度（字符数），超过时会尝试进一步压缩
MAX_SUMMARY_LENGTH = 2000
