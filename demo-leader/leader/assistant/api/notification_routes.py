"""
Leader · 通知回调路由注册

将 NotificationExecutor 构建的 NotificationReceiver 挂载到 FastAPI 应用，
提供 Leader 侧的通知回调端点。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from assistant.core.notification_executor import NotificationExecutor


def register_notification_routes(
    app: FastAPI,
    executor: NotificationExecutor,
    path_prefix: str = "/aip/notifications",
) -> None:
    """向 FastAPI 应用挂载通知回调端点。

    每个 task 的回调 URL 格式为：{path_prefix}/{task_id}

    Args:
        app: Leader FastAPI 应用实例
        executor: NotificationExecutor 实例（提供 token 校验与回调处理）
        path_prefix: 回调路由前缀（默认 /aip/notifications）
    """
    receiver = executor.build_receiver()
    callback_path = f"{path_prefix}/{{task_id}}"
    receiver.mount(app, callback_path)
