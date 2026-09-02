"""
Partner 流式传输桥接器

StreamHandler 将 GenericRunner 的任务状态变化事件桥接到 StreamHub（SSE 通道），
供 Partner /stream 端点的 SSE 订阅者消费。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from acps_sdk.aip.aip_base_model import TaskResult, TaskState
from acps_sdk.aip.aip_stream_server import StreamHub

if TYPE_CHECKING:
    from partners.generic_runner import GenericRunner

logger = logging.getLogger("partners.stream_handler")

# 终态集合（同 SDK aip_stream_server）
_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.Completed, TaskState.Canceled, TaskState.Failed, TaskState.Rejected}
)


class StreamHandler:
 """将 GenericRunner 状态变化发布到 StreamHub。

    在 Partner 的 FastAPI lifespan 中创建此对象，并将 hub 传递给
    add_aip_stream_router。

    on_stream_start 回调由 SDK 的 handle_stream_request 在收到
    'start' 命令时调用，此时 Handler 需要确保 hub 中有对应通道，
    然后将 command 转发给 runner（真正的业务逻辑入口）。
 """

    def __init__(self, runner: GenericRunner) -> None:
        self.runner = runner
        self.hub = StreamHub
        self._bg_tasks: set[asyncio.Task[Any]] = set
 # 注册状态变更监听者
        runner.add_state_change_listener(self._on_runner_state_change)

    async def on_stream_start(self, command: object) -> None:
 """收到 'stream/start' 命令时的回调（由 add_aip_stream_router 调用）。

        1. 确保 hub 中存在该任务的 StreamChannel
        2. 将命令转发给 runner.on_start（开始业务处理）
 """
        from acps_sdk.aip.aip_base_model import TaskCommand

        assert isinstance(command, TaskCommand), f"Expected TaskCommand, got {type(command)}"
        task_id = command.taskId
        if not task_id:
            return

 # 确保通道存在（on_stream_start 在路由注册通道之后被调用，
 # 但此处再次 get_or_create 是幂等的）
        self.hub.get_or_create_channel(task_id)

 # 调用 runner 处理任务（fire-and-forget，状态通过 listener 发布回 hub）
        _t = asyncio.create_task(self.runner.on_start(command, None))
        self._bg_tasks.add(_t)
        _t.add_done_callback(self._bg_tasks.discard)

    async def _on_runner_state_change(self, task_result: TaskResult) -> None:
 """GenericRunner 状态变化时将 TaskResult 发布到 StreamHub 对应通道。"""
        task_id = task_result.taskId
        is_terminal = task_result.status.state in _TERMINAL_STATES

        try:
            await self.hub.publish_task_result(task_id, task_result, is_terminal=is_terminal)
        except Exception as exc:
            logger.warning(
                "StreamHandler: failed to publish task result to hub: task_id=%s error=%s",
                task_id,
                str(exc),
            )

 # 终态时关闭通道并从 hub 中移除，防止通道泄漏
        if is_terminal:
            try:
                await self.hub.close_stream(task_id)
            except Exception as exc:
                logger.warning(
                    "StreamHandler: failed to close stream channel: task_id=%s error=%s",
                    task_id,
                    str(exc),
                )
