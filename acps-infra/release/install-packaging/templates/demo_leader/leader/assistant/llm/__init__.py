"""
Leader Agent Platform - LLM Layer

本模块提供 LLM 调用相关的实现。

模块结构：
- schemas: LLM 输入/输出的 Pydantic 模型（LLM-1 到 LLM-7）
- client: LLM API 客户端
"""

from .client import LLMClient, get_llm_client

__all__ = [
    "LLMClient",
    "get_llm_client",
]
