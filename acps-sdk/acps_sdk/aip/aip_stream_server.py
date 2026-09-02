"""
AIP v2 流式传输服务端实现

本模块分两层：
  S1 层（纯逻辑，无 HTTP 依赖）：
    - BufferedStreamEvent   —— 带序列号的缓冲事件
    - TaskStreamChannel     —— 单任务事件通道（发布/回放/订阅/关闭）
    - StreamHub             —— 多任务通道管理器

  S2 层（HTTP / FastAPI，在此文件后半段实现）：
    - format_sse            —— StreamResponse → bytes
    - build_stream_response —— 构建正常事件帧
    - build_stream_error_response —— 构建错误帧
    - StreamHandlers        —— 应用层回调占位符
    - handle_stream_request —— FastAPI 路由处理函数
    - _sse_event_generator  —— AsyncIterator[bytes]
    - add_aip_stream_router —— 注册 FastAPI 路由
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Optional

from .aip_base_model import TaskResult, TaskState
from .aip_identity import (
    AipIdentityError,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    identity_error_to_jsonrpc,
)
from .aip_peer_cert import get_request_peer_aic
from .aip_stream_model import (
    ProductChunkEvent,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    StreamEventData,
    StreamEventPayload,
    StreamResponse,
    TaskStatusUpdateEvent,
)

logger = logging.getLogger("acps_sdk.aip.aip_stream_server")

# 终态集合（用于判断是否需要关闭通道）
_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.Completed, TaskState.Canceled, TaskState.Failed, TaskState.Rejected}
)


# ===========================================================================
# S1：纯逻辑层
# ===========================================================================


@dataclass
class BufferedStreamEvent:
    """带序列号的缓冲事件。"""

    event_seq: int
    payload: StreamEventPayload
    is_terminal: bool = False


class TaskStreamChannel:
    """单任务 SSE 事件通道。

    职责：
    - 维护单调递增序列号
    - 以环形缓冲（deque）存储历史事件供重连时回放
    - 支持多个并发订阅者（每个订阅者独立的 asyncio.Queue）
    - 终态事件发布后自动关闭通道
    """

    def __init__(self, max_buffer: int = 200) -> None:
        self._max_buffer = max_buffer
        self._buffer: collections.deque[BufferedStreamEvent] = collections.deque(
            maxlen=max_buffer
        )
        self._next_seq: int = 1
        self._closed: bool = False
        # 订阅者队列列表
        self._queues: list[asyncio.Queue[BufferedStreamEvent | None]] = []
        self._lock = asyncio.Lock()

    # --- 属性 ---

    @property
    def latest_seq(self) -> int:
        """已发布的最大序列号（尚未发布任何事件时为 0）。"""
        return self._next_seq - 1

    @property
    def oldest_buffered_seq(self) -> int:
        """缓冲区中最旧事件的序列号（缓冲为空时为 1）。"""
        return self._buffer[0].event_seq if self._buffer else 1

    @property
    def is_closed(self) -> bool:
        return self._closed

    # --- 发布 ---

    async def publish(
        self,
        payload: StreamEventPayload,
        *,
        is_terminal: bool = False,
    ) -> BufferedStreamEvent:
        """发布一条事件；终态事件发布后自动关闭通道。"""
        async with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            event = BufferedStreamEvent(
                event_seq=seq, payload=payload, is_terminal=is_terminal
            )
            self._buffer.append(event)
            # 分发给所有订阅者
            for q in self._queues:
                await q.put(event)

        if is_terminal:
            await self.close()

        return event

    # --- 回放 ---

    def replay(
        self, last_event_seq: Optional[int]
    ) -> list[BufferedStreamEvent]:
        """返回 seq > last_event_seq 的缓冲事件列表；None 表示返回全部。"""
        if last_event_seq is None:
            return list(self._buffer)
        return [e for e in self._buffer if e.event_seq > last_event_seq]

    # --- 可续传判断 ---

    def can_resume(self, last_event_seq: Optional[int]) -> bool:
        """判断 last_event_seq 对应的重连是否可续传。

        - None：始终可续传（从头重放）
        - last_event_seq >= oldest_buffered_seq - 1：可续传
        - 否则：缓冲已溢出，不可续传
        """
        if last_event_seq is None:
            return True
        if not self._buffer:
            return True
        return last_event_seq >= self.oldest_buffered_seq - 1

    # --- 订阅 ---

    async def subscribe(
        self, last_event_seq: Optional[int] = None
    ) -> AsyncIterator[BufferedStreamEvent]:
        """订阅事件流。

        先 yield 缓冲中的历史事件（重放），再 yield 实时事件。
        通道关闭时迭代器正常结束（通过哨兵 None 触发）。

        若通道在订阅前已关闭，则 yield 重放历史后直接返回，不阻塞等待。
        """
        q: asyncio.Queue[BufferedStreamEvent | None] = asyncio.Queue()

        # 注册订阅者队列（已关闭则不注册，避免永久阻塞）
        async with self._lock:
            replayed = self.replay(last_event_seq)
            already_closed = self._closed
            if not already_closed:
                self._queues.append(q)

        # 先 yield 重放事件
        for event in replayed:
            yield event

        # 若订阅时通道已关闭，无需等待新事件
        if already_closed:
            return

        # 再 yield 实时事件
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
        finally:
            # 清理：移除队列
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    # --- 关闭 ---

    async def close(self) -> None:
        """关闭通道，向所有订阅者发送哨兵 None 以结束迭代。"""
        if self._closed:
            return
        self._closed = True
        for q in self._queues:
            await q.put(None)


# ===========================================================================
# StreamHub：多任务通道管理
# ===========================================================================


class StreamHub:
    """管理所有活跃任务的 TaskStreamChannel。

    每个任务 ID 对应一个通道；Partner 通过 StreamHub 发布事件；
    aip_stream_server 的 HTTP 层通过 StreamHub 获取通道并订阅。
    """

    def __init__(self, max_buffer: int = 200) -> None:
        self._channels: dict[str, TaskStreamChannel] = {}
        self._max_buffer = max_buffer

    def get_or_create_channel(self, task_id: str) -> TaskStreamChannel:
        """获取已有通道或创建新通道。"""
        if task_id not in self._channels:
            self._channels[task_id] = TaskStreamChannel(max_buffer=self._max_buffer)
        return self._channels[task_id]

    def get_channel(self, task_id: str) -> Optional[TaskStreamChannel]:
        """获取已有通道，不存在则返回 None。"""
        return self._channels.get(task_id)

    def remove_channel(self, task_id: str) -> None:
        """从 Hub 中移除通道记录（不关闭通道本身）。"""
        self._channels.pop(task_id, None)

    async def publish_task_result(
        self,
        task_id: str,
        task_result: TaskResult,
        *,
        is_terminal: Optional[bool] = None,
    ) -> None:
        """发布 TaskResult 事件；is_terminal=None 时自动按状态判断。"""
        ch = self.get_or_create_channel(task_id)
        if is_terminal is None:
            is_terminal = task_result.status.state in _TERMINAL_STATES
        await ch.publish(task_result, is_terminal=is_terminal)

    async def publish_status_update(
        self, task_id: str, event: "TaskStatusUpdateEvent"
    ) -> None:
        ch = self.get_or_create_channel(task_id)
        await ch.publish(event, is_terminal=False)

    async def publish_product_chunk(
        self, task_id: str, event: "ProductChunkEvent"
    ) -> None:
        ch = self.get_or_create_channel(task_id)
        await ch.publish(event, is_terminal=False)

    async def close_stream(self, task_id: str) -> None:
        """关闭并移除通道。"""
        ch = self._channels.pop(task_id, None)
        if ch is not None:
            await ch.close()


# ===========================================================================
# S2：HTTP / FastAPI 层
# ===========================================================================

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from .aip_base_model import TaskCommandType
from .aip_rpc_model import JSONRPCError
from .aip_stream_model import StreamRequest, StreamResponse


def format_sse(response: StreamResponse) -> bytes:
    """将 StreamResponse 序列化为 SSE 数据行 bytes。

    格式：b"data: <json>\\n\\n"
    JSON 使用 exclude_none=True 省略空字段。
    """
    json_str = response.model_dump_json(exclude_none=True)
    return f"data: {json_str}\n\n".encode()


def build_stream_response(rpc_id: str | None, event: BufferedStreamEvent) -> StreamResponse:
    """构建正常事件帧 StreamResponse（含 result，不含 error）。"""
    event_data = StreamEventData(eventSeq=event.event_seq, eventData=event.payload)
    return StreamResponse(id=rpc_id, result=event_data)


def build_stream_error_response(
    rpc_id: str | None,
    code: int,
    message: str,
    data: object = None,
) -> StreamResponse:
    """构建纯错误帧 StreamResponse（result 为 None，含 error）。"""
    error = JSONRPCError(code=code, message=message, data=data)
    return StreamResponse(id=rpc_id, error=error)


@dataclass
class StreamHandlers:
    """可插拔的流处理回调（与 RPC 的 CommandHandlers 对应）。

    on_stream_start: 收到 stream(start) 时调用——业务层据此启动任务，
                     后台通过 hub.publish_* 注入事件。
    on_re_stream:    收到 re-stream 且通道不存在时调用（可选）——
                     业务层可尝试恢复任务/通道；返回 None 则走 404 逻辑。
    """

    on_stream_start: Callable[[object], Awaitable[None]]
    on_re_stream: Optional[Callable[[object], Awaitable[Optional[TaskStreamChannel]]]] = None


async def _sse_event_generator(
    hub: StreamHub,
    task_id: str,
    rpc_id: str | None,
    last_event_seq: Optional[int] = None,
    *,
    local_aic: str | None = None,
    identity_binding_enabled: bool = False,
) -> AsyncIterator[bytes]:
    """异步生成器：从通道订阅事件并 yield SSE bytes。"""
    channel = hub.get_channel(task_id)
    if channel is None:
        # 通道不存在：发送错误帧并结束
        yield format_sse(
            build_stream_error_response(rpc_id, -32001, f"Task stream not found: {task_id}")
        )
        return

    async for event in channel.subscribe(last_event_seq=last_event_seq):
        if identity_binding_enabled:
            assert_sender_matches_expected(event.payload, local_aic)
        yield format_sse(build_stream_response(rpc_id, event))


async def handle_stream_request(
    request: Request,
    hub: StreamHub,
    handlers: "StreamHandlers | Callable[[object], Awaitable[None]]",
    *,
    local_aic: str | None = None,
    identity_binding_enabled: bool = True,
) -> StreamingResponse:
    """FastAPI 路由处理函数：解析 StreamRequest，校验命令类型，返回 SSE 流。

    handlers 接受两种形式：
    - StreamHandlers dataclass（推荐，支持 on_re_stream 钩子）
    - 单个 Callable（向下兼容旧接口，等价于 on_stream_start）
    """
    if isinstance(handlers, StreamHandlers):
        on_stream_start = handlers.on_stream_start
        on_re_stream = handlers.on_re_stream
    else:
        on_stream_start = handlers
        on_re_stream = None

    try:
        body = await request.json()
        stream_request = StreamRequest.model_validate(body)
    except (ValidationError, ValueError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": -32700, "message": "Parse error", "data": str(exc)},
        )

    command = stream_request.params.message
    task_id = command.taskId
    rpc_id = stream_request.id

    if identity_binding_enabled and not local_aic:
        raise ValueError("local_aic is required when identity_binding_enabled=True")

    if identity_binding_enabled:
        try:
            assert_sender_matches_peer(command, get_request_peer_aic(request))
        except AipIdentityError as exc:
            error = identity_error_to_jsonrpc(exc)
            status_code = 401 if error.code == -32008 else 403
            raise HTTPException(
                status_code=status_code,
                detail=error.model_dump(exclude_none=True),
            ) from exc

    # 支持的起始命令：start（新任务）
    # re-stream 不走此分支（由客户端在已有连接上发送）
    if command.command == TaskCommandType.Start:
        if not task_id:
            raise HTTPException(
                status_code=400,
                detail={"code": -32602, "message": "taskId is required for stream/start"},
            )
        # 确保通道存在（在 on_stream_start 异步触发任务处理之前先创建通道）
        hub.get_or_create_channel(task_id)
        # 异步启动任务处理（fire-and-forget）
        asyncio.create_task(on_stream_start(command))
        return StreamingResponse(
            _sse_event_generator(
                hub=hub,
                task_id=task_id,
                rpc_id=rpc_id,
                local_aic=local_aic,
                identity_binding_enabled=identity_binding_enabled,
            ),
            media_type=SSE_MEDIA_TYPE,
            headers=SSE_HEADERS,
        )

    elif command.command == TaskCommandType.ReStream:
        last_event_seq: Optional[int] = None
        if command.commandParams:
            last_event_seq = command.commandParams.get("lastEventSeq")
        channel = hub.get_channel(task_id)
        if channel is None and on_re_stream is not None:
            # 业务层有 on_re_stream 钩子，尝试恢复通道
            channel = await on_re_stream(command)
        if channel is None:
            raise HTTPException(
                status_code=404,
                detail={"code": -32001, "message": f"Stream not found: {task_id}"},
            )
        if not channel.can_resume(last_event_seq):
            # 无法续传：单帧错误响应
            async def _error_gen() -> AsyncIterator[bytes]:
                yield format_sse(
                    build_stream_error_response(
                        rpc_id,
                        -32002,
                        "Cannot re-stream: buffer overflow, please restart",
                    )
                )

            return StreamingResponse(
                _error_gen(),
                media_type=SSE_MEDIA_TYPE,
                headers=SSE_HEADERS,
            )
        return StreamingResponse(
            _sse_event_generator(
                hub=hub,
                task_id=task_id,
                rpc_id=rpc_id,
                last_event_seq=last_event_seq,
                local_aic=local_aic,
                identity_binding_enabled=identity_binding_enabled,
            ),
            media_type=SSE_MEDIA_TYPE,
            headers=SSE_HEADERS,
        )

    else:
        raise HTTPException(
            status_code=400,
            detail={
                "code": -32602,
                "message": f"Unsupported stream command: {command.command}",
            },
        )


def add_aip_stream_router(
    app: FastAPI,
    endpoint: str,
    hub: StreamHub,
    handlers: "StreamHandlers | Callable[[object], Awaitable[None]]",
    *,
    local_aic: str | None = None,
    identity_binding_enabled: bool = True,
) -> None:
    """向 FastAPI 应用注册 AIP 流式端点。

    handlers 接受 StreamHandlers（推荐）或单个 Callable（向下兼容）。
    """

    if not identity_binding_enabled:
        logger.warning(
            "AIP identity binding disabled for Stream server endpoint=%s",
            endpoint,
        )

    @app.post(endpoint)
    async def stream_endpoint(request: Request) -> StreamingResponse:
        return await handle_stream_request(
            request,
            hub,
            handlers,
            local_aic=local_aic,
            identity_binding_enabled=identity_binding_enabled,
        )
