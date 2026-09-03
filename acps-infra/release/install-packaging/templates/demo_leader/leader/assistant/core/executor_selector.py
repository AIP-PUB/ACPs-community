"""
Leader · 执行器选择器

根据 Partner ACS 能力位（capabilities）选择合适的执行器策略：
- streaming=true  → StreamExecutor（SSE 推流）
- notification=true → NotificationExecutor（异步推送回调）
- 否则            → TaskExecutor（RPC 轮询，向后兼容）
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)


class ExecutorStrategy(str, Enum):
 """执行器策略枚举。"""

    STREAM = "stream"
    NOTIFICATION = "notification"
    RPC = "rpc"


def select_executor_strategy(acs_data: dict[str, Any]) -> ExecutorStrategy:
 """根据 Partner ACS 数据返回应使用的执行器策略。

    优先级：streaming > notification > rpc（向后兼容）

    Args:
        acs_data: Partner 的 ACS（Agent Capability Specification）字典

    Returns:
        ExecutorStrategy 枚举值
 """
    capabilities = acs_data.get("capabilities", {})

    if capabilities.get("streaming", False):
        return ExecutorStrategy.STREAM
    if capabilities.get("notification", False):
        return ExecutorStrategy.NOTIFICATION
    return ExecutorStrategy.RPC


def build_executor_for_partner(
    acs_data: dict[str, Any],
    leader_id: str,
    partner_base_url: str,
    *,
    callback_base_url: str | None = None,
    ssl_context: Any = None,
) -> Any:
 """为单个 Partner 构建最合适的执行器实例。

    Args:
        acs_data: Partner ACS 字典（用于读取能力位）
        leader_id: Leader 的 AIC
        partner_base_url: Partner 的 HTTP 基地址（不含路径）
        callback_base_url: Leader 侧通知回调的基地址（NotificationExecutor 所需）
        ssl_context: 可选的 mTLS SSL 上下文

    Returns:
        StreamExecutor | NotificationExecutor | TaskExecutor 实例
 """
    strategy = select_executor_strategy(acs_data)

    if strategy == ExecutorStrategy.STREAM:
        from .stream_executor import StreamExecutor

        logger.info("Selecting StreamExecutor for partner_base_url=%s", partner_base_url)
        return StreamExecutor(
            partner_base_url=partner_base_url,
            leader_id=leader_id,
            expected_partner_aic=acs_data.get("aic"),
            ssl_context=ssl_context,
        )

    if strategy == ExecutorStrategy.NOTIFICATION:
        from .notification_executor import NotificationExecutor

        if not callback_base_url:
            logger.warning(
                "NotificationExecutor selected but callback_base_url is not set; "
                "falling back to RPC executor for partner_base_url=%s",
                partner_base_url,
            )
        else:
            logger.info("Selecting NotificationExecutor for partner_base_url=%s", partner_base_url)
            return NotificationExecutor(
                partner_base_url=partner_base_url,
                leader_id=leader_id,
                callback_base_url=callback_base_url,
                expected_partner_aic=acs_data.get("aic"),
                ssl_context=ssl_context,
            )

 # 默认 RPC 轮询执行器（向后兼容）
    from .executor import ExecutorConfig, TaskExecutor

    logger.info("Selecting TaskExecutor (RPC) for partner_base_url=%s", partner_base_url)
    return TaskExecutor(
        leader_aic=leader_id,
        config=ExecutorConfig,
        ssl_context=ssl_context,
    )
