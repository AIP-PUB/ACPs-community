"""
Leader · StreamEventBus

会话级别的流式事件总线，将 AipExecutor 的 SSE 事件转发给
Leader API 的 /api/v1/stream/{session_id} 端点。

设计：
- AipExecutor 在执行 streaming partner 时，调用 bus.push(session_id, event)
- Leader API SSE 端点通过 bus.subscribe(session_id) 获得 Queue 并消费
- None 作为哨兵值：表示该 session 的流式传输已结束
- 支持多消费者（每次 subscribe 创建独立 Queue），适用于 Tab 重连场景
- 延迟订阅者保障：若流已结束后再订阅，立刻收到哨兵 None（避免竞态挂起）

线程安全：仅用于 asyncio 事件循环内，不加锁。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from acps_sdk.aip.aip_stream_model import StreamResponse

logger = logging.getLogger(__name__)

# 已结束流的 session 在 bus 中保留的最长时间（秒），过期后自动清理
_DONE_TTL_S = 300.0


class StreamEventBus:
    """会话级别 SSE 事件总线。"""

    def __init__(self) -> None:
        # session_id → list of subscriber Queues（活跃订阅）
        self._subscribers: dict[str, list[asyncio.Queue[StreamResponse | None]]] = defaultdict(list)
        # session_id → 流结束的时间戳（用于延迟订阅者保障 + TTL 清理）
        self._done_at: dict[str, float] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue[StreamResponse | None]:
        """为 session 注册一个订阅者队列，返回该 Queue 用于消费事件。

        同一 session 允许多个订阅者（多标签页 / 重连场景）。
        若流已结束（sentinel 已推送），立刻在队列中放入 None，让消费方快速退出。
        """
        queue: asyncio.Queue[StreamResponse | None] = asyncio.Queue()
        if session_id in self._done_at:
            # 流已结束，延迟订阅者立刻收到哨兵，无需加入活跃订阅列表
            queue.put_nowait(None)
            logger.debug("[StreamEventBus] subscribe session=%s (already done, sentinel pre-filled)", session_id)
        else:
            self._subscribers[session_id].append(queue)
            logger.debug(
                "[StreamEventBus] subscribe session=%s (total=%d)",
                session_id,
                len(self._subscribers[session_id]),
            )
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[StreamResponse | None]) -> None:
        """移除订阅者队列。"""
        queues = self._subscribers.get(session_id, [])
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._subscribers.pop(session_id, None)
        logger.debug("[StreamEventBus] unsubscribe session=%s (remaining=%d)", session_id, len(queues))

    def push(self, session_id: str, event: StreamResponse | None) -> None:
        """将事件推送给 session 的所有订阅者。

        event=None 表示流结束（哨兵），消费方收到后应停止读取。
        推送哨兵时同时在 _done_at 中记录时间，保障延迟到来的订阅者。
        """
        if event is None:
            self._done_at[session_id] = time.monotonic()
            self._cleanup_expired()

        queues = self._subscribers.get(session_id)
        if not queues:
            return
        for q in list(queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("[StreamEventBus] queue full for session=%s, dropping event", session_id)

    def has_subscribers(self, session_id: str) -> bool:
        """返回该 session 是否有活跃订阅者。"""
        return bool(self._subscribers.get(session_id))

    def _cleanup_expired(self) -> None:
        """清理超过 TTL 的已结束 session 记录（避免内存泄漏）。"""
        now = time.monotonic()
        expired = [sid for sid, ts in self._done_at.items() if now - ts > _DONE_TTL_S]
        for sid in expired:
            del self._done_at[sid]
            self._subscribers.pop(sid, None)


# 进程级单例
_bus: StreamEventBus | None = None


def get_stream_event_bus() -> StreamEventBus:
    """获取全局 StreamEventBus 单例。"""
    global _bus
    if _bus is None:
        _bus = StreamEventBus()
    return _bus
