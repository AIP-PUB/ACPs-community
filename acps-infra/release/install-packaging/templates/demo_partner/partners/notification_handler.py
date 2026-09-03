"""
Partner 通知方式桥接器

NotificationHandler 将 GenericRunner 的任务状态变化事件通过
NotificationService（NotificationDispatcher）推送到 Leader 注册的回调 URL。
"""

from __future__ import annotations

import logging
import ssl
from typing import TYPE_CHECKING

import httpx
from acps_sdk.aip.aip_base_model import TaskResult
from acps_sdk.aip.aip_notification_server import NotificationService

if TYPE_CHECKING:
    from partners.generic_runner import GenericRunner

logger = logging.getLogger("partners.notification_handler")


class NotificationHandler:
 """将 GenericRunner 状态变化分发给已注册的通知订阅者。

    在 Partner 的 FastAPI lifespan 中创建此对象；
    add_aip_notification_router 使用 handler.service 提供四个端点。
 """

    def __init__(
        self,
        runner: GenericRunner,
        *,
        service: NotificationService | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        local_aic: str | None = None,
        callback_ssl_context: ssl.SSLContext | None = None,
        identity_binding_enabled: bool = True,
    ) -> None:
        self.runner = runner
 # 当前 demo 使用「双阶段」设计：Leader 先通过 /rpc 启动任务（runner.on_start 由此触发），
 # 再通过 /notification/start 注册订阅；此处不注册 on_notification_start，避免重复启动。
 # 若需「单阶段」设计（单次 notification/start 同时启动并订阅），
 # 可传入 NotificationHandlers(on_notification_start=...) 并自行保证幂等。
        self.service = service or NotificationService(
            local_aic=local_aic,
            callback_ssl_context=callback_ssl_context,
            transport=transport,
            identity_binding_enabled=identity_binding_enabled,
        )
 # 注册状态变更监听者
        runner.add_state_change_listener(self._on_runner_state_change)

    async def _on_runner_state_change(self, task_result: TaskResult) -> None:
 """GenericRunner 状态变化时触发通知分发。"""
        try:
            await self.service.dispatch(task_result)
        except Exception as exc:
            logger.warning(
                "NotificationHandler: dispatch failed: task_id=%s error=%s",
                task_result.taskId,
                str(exc),
            )

    async def close(self) -> None:
 """释放 service 内部资源。"""
        await self.service.close
