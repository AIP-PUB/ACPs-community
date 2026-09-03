"""
Leader · StreamExecutor

通过 AIP Streaming（SSE）方式与 Partner 交互的执行器。

用法：
    executor = StreamExecutor(partner_base_url="https://partner/", leader_id="leader-1")
    final = await executor.run(session_id, user_input, on_event=my_callback, task_id=task_id)
    await executor.close
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Optional

import httpx
from acps_sdk.aip.aip_base_model import TaskResult, TaskState
from acps_sdk.aip.aip_rpc_client import AipRpcClient
from acps_sdk.aip.aip_stream_client import AipStreamClient
from acps_sdk.aip.aip_stream_model import StreamResponse

logger = logging.getLogger(__name__)

_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.Completed, TaskState.Canceled, TaskState.Failed, TaskState.Rejected}
)
_STABLE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AwaitingInput,
        TaskState.AwaitingCompletion,
        TaskState.Completed,
        TaskState.Canceled,
        TaskState.Failed,
        TaskState.Rejected,
    }
)

OnEventCallback = Callable[[StreamResponse], Coroutine[Any, Any, None]]


class StreamExecutor:
 """Leader 侧流式执行器。

    - `run`: 消费 SSE 流；对每个事件调用 on_event；返回最后一个稳定态 TaskResult。
    - `complete` / `cancel`: 走 RPC 端点（不走 SSE）。
    - `close`: 释放 httpx 连接池。
 """

    def __init__(
        self,
        partner_base_url: str,
        leader_id: str,
        *,
        expected_partner_aic: str | None = None,
        identity_binding_enabled: bool = True,
        ssl_context: Any = None,
        sse_transport: httpx.AsyncTransport | None = None,
        rpc_transport: httpx.AsyncTransport | None = None,
    ) -> None:
        base = partner_base_url.rstrip("/")
        stream_url = f"{base}/stream"
        rpc_url = f"{base}/rpc"

 # AipStreamClient：只用于 SSE 流，不调用其内部 complete/cancel
        self.stream_client = AipStreamClient(
            partner_stream_url=stream_url,
            partner_rpc_url=rpc_url,
            leader_id=leader_id,
            transport=sse_transport,
            ssl_context=ssl_context,
            expected_partner_aic=expected_partner_aic,
            identity_binding_enabled=identity_binding_enabled,
        )

 # 独立的 RPC 客户端，用于 complete / cancel 操作
        self._rpc_client = AipRpcClient(
            partner_url=rpc_url,
            leader_id=leader_id,
            ssl_context=ssl_context,
            transport=rpc_transport,
            expected_partner_aic=expected_partner_aic,
            identity_binding_enabled=identity_binding_enabled,
        )

    async def run(
        self,
        session_id: str,
        user_input: str,
        on_event: OnEventCallback,
        *,
        task_id: str | None = None,
        reconnect: bool = True,
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 1.0,
    ) -> TaskResult | None:
 """消费 SSE 流，对每个事件调用 on_event，返回稳定态 TaskResult。

        Args:
            session_id: 会话 ID
            user_input: 用户输入文本（映射到 AipStreamClient 的 text_content）
            on_event: 每个 SSE 事件的异步回调
            task_id: 可选任务 ID（默认自动生成 UUID）
            reconnect: True 时启用断线重连（via stream_with_reconnect）
            max_reconnects: 最大重连次数（reconnect=True 时有效）
            reconnect_backoff_s: 重连初始退避秒数（指数退避）

        Returns:
            最后一个稳定态 TaskResult，或无稳定态时的最后一个 TaskResult
 """
        if not task_id:
            task_id = str(uuid.uuid4)

        final_task_result: TaskResult | None = None
        last_task_result: TaskResult | None = None

        if reconnect:
            stream_iter = self.stream_client.stream_with_reconnect(
                session_id=session_id,
                user_input=user_input,
                task_id=task_id,
                max_reconnects=max_reconnects,
                backoff_s=reconnect_backoff_s,
            )
        else:
            stream_iter = self.stream_client.start_stream(
                session_id=session_id,
                task_id=task_id,
                text_content=user_input,
            )

        async for event in stream_iter:
            try:
                await on_event(event)
            except Exception as exc:
                logger.warning("StreamExecutor.run: on_event callback raised: %s", str(exc))

            if event.result is not None:
                ed = event.result.eventData
                if isinstance(ed, TaskResult):
                    last_task_result = ed
                    if ed.status.state in _STABLE_STATES:
                        final_task_result = ed
                        break

        return final_task_result or last_task_result

    async def complete(self, task_id: str, session_id: str) -> None:
 """通过 RPC 通知 Partner 完成任务（不走 SSE）。"""
        await self._rpc_client.complete_task(task_id=task_id, session_id=session_id)

    async def cancel(self, task_id: str, session_id: str) -> None:
 """通过 RPC 取消 Partner 任务（不走 SSE）。"""
        await self._rpc_client.cancel_task(task_id=task_id, session_id=session_id)

    async def close(self) -> None:
 """释放所有内部 HTTP 连接池。"""
        try:
            await self.stream_client.close
        except Exception as exc:
            logger.warning("StreamExecutor: error closing stream client: %s", str(exc))
        try:
            await self._rpc_client.close
        except Exception as exc:
            logger.warning("StreamExecutor: error closing rpc client: %s", str(exc))
