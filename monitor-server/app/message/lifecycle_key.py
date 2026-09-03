"""app/message/lifecycle_key.py — 生命周期键推导（纯函数，C-MESSAGE-WRITE-3）。

相同输入必产生相同键（C-MESSAGE-MODEL-2）。
禁止用不稳定字段组合伪造键；无法稳定构造时返回 ''（只进 message_events，不进 message_lifecycle）。
"""

from __future__ import annotations

from typing import Final

MID_PREFIX: Final = "mid:"
CID_PREFIX: Final = "cid:"


def compute_lifecycle_key(
    *,
    message_id: str | None,
    correlation_id: str | None,
    correlation_id_stable_unique: bool,
) -> str:
    """根据设计 §2.3 推导规则计算 lifecycle_key。

    推导顺序（严格按优先级）：
    1. message_id 非空 → 'mid:' + message_id
    2. message_id 缺失 且 correlation_id_stable_unique=True 且 correlation_id 非空 → 'cid:' + correlation_id
    3. 其它 → ''（该事件只进 message_events，不进 message_lifecycle）

    Args:
        message_id: 来自 MessageBody.message_id（可为 None / 空串）。
        correlation_id: 来自 LogRecord 顶层（非 body）。
        correlation_id_stable_unique: 部署声明：correlationId 在此部署中稳定唯一。
    """
    if message_id:
        return MID_PREFIX + message_id
    if correlation_id_stable_unique and correlation_id:
        return CID_PREFIX + correlation_id
    return ""


def is_synthetic(lifecycle_key: str) -> bool:
    """生命周期键是否为合成键（cid: 前缀）。

    cid: 前缀的键基于 correlation_id 推导而来，属部署配置声明稳定唯一的「合成」键，
    用于指标 amp_message_writer_synthetic_lifecycle_keys_total（设计 §6.2/§6.13）。
    空串 lifecycle_key 不计入此指标（直接不进 lifecycle 表，由 compactor SQL 过滤）。
    """
    return lifecycle_key.startswith(CID_PREFIX)


def message_id_to_lifecycle_key(message_id: str) -> str:
    """将 messageId 转换为 lifecycle_key（用于 lifecycles/{messageId} 端点，C-MESSAGE-QUERY-7）。

    按 ORDER BY 键过滤而非派生 message_id 列，避免全表扫描。
    """
    return MID_PREFIX + message_id
