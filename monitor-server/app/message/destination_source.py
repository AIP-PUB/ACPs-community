"""app/message/destination_source.py — 目的地状态采集源接口（设计 §5.2，可插拔）。

定义 collector 与具体 broker 解耦的 DestinationStateSource 协议 + 默认空实现。
真实 broker adapter（RabbitMQ / Kafka 等）按部署注入，不在核心范围（§12 O-2）。
默认 null 源使 destinations 端点在无采集时返回明确 503（C-MESSAGE-QUERY-4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings


@dataclass(frozen=True)
class DestinationSample:
    """单目的地状态样本（对应 message_destination_state_snapshot 一行）。"""

    system: str
    destination_name: str
    destination_kind: str
    virtual_host: str
    visible_messages: int | None
    inflight_messages: int | None
    delayed_messages: int | None
    dead_letter_messages: int | None
    oldest_message_age_seconds: int | None
    active_consumers: int | None
    size_bytes: int | None
    captured_at_ms: int  # 采样时刻（= 快照 captured_at）


@runtime_checkable
class DestinationStateSource(Protocol):
    """broker 状态采集接口。"""

    async def sample(self) -> list[DestinationSample]:
        """拉取当前所有目的地状态快照。"""
        ...


class NullDestinationStateSource:
    """默认实现：返回 []（destinations/query 一律 STATE_SNAPSHOT_UNAVAILABLE 503）。"""

    async def sample(self) -> list[DestinationSample]:
        return []


def build_destination_source(cfg: Any = None) -> DestinationStateSource:
    """工厂：按 settings.message_destination_source_kind 选择实现。

    首版只注册 null（§12 O-2 预留扩展点）。
    """
    kind = (cfg or settings).message_destination_source_kind if cfg else settings.message_destination_source_kind
    if kind == "null" or not kind:
        return NullDestinationStateSource()
    # 未来扩展点：按 kind 注册真实 adapter
    return NullDestinationStateSource()
