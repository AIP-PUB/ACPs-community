"""
Leader · NotificationExecutor

通过 AIP Notification 方式与 Partner 交互的执行器。

工作流：
1. `start` / `start_for_partner`: 生成安全 token → 注册 notification 配置 → RPC 启动任务 → 注册订阅
2. Partner 状态变化时推送回调 → Leader 端 `on_callback` 处理
3. `build_receiver`: 构建 FastAPI 回调端点（挂载到 Leader 应用）
4. `register_task_future`: 返回 asyncio.Future，在收到终态回调时自动 resolve（供 AipExecutor 等待）
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Coroutine
from typing import Any, Optional

from acps_sdk.aip.aip_base_model import TaskResult, TaskState
from acps_sdk.aip.aip_notification_client import AipNotificationClient, NotificationReceiver
from acps_sdk.aip.aip_rpc_client import AipRpcClient

logger = logging.getLogger(__name__)

TaskResultCallback = Callable[[TaskResult], Coroutine[Any, Any, None]]

_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.Completed, TaskState.Canceled, TaskState.Failed, TaskState.Rejected}
)


class NotificationExecutor:
 """Leader 侧通知方式执行器。

    - `start` / `start_for_partner`: 生成 token、注册回调、启动任务、订阅通知。
    - `register_task_future`: 返回 Future，收到该 task 终态回调时自动 resolve。
    - `on_callback`: 收到 Partner 推送时的入口（由 NotificationReceiver 路由）。
    - `build_receiver`: 构建 FastAPI 回调接收端点。
    - `close`: 释放内部 HTTP 客户端。
 """

    def __init__(
        self,
        partner_base_url: str,
        leader_id: str,
        callback_base_url: str,
        *,
        expected_partner_aic: str | None = None,
        identity_binding_enabled: bool = True,
        ssl_context: Any = None,
        transport: Any | None = None,
    ) -> None:
        base = partner_base_url.rstrip("/")
        self._partner_notification_url = f"{base}/notification"
        self._partner_rpc_url = f"{base}/rpc"
        self._leader_id = leader_id
        self._callback_base_url = callback_base_url.rstrip("/")
        self._expected_partner_aic = expected_partner_aic
        self._identity_binding_enabled = identity_binding_enabled
        self._ssl_context = ssl_context
        self._transport = transport

        self._client = None
        self._rpc_client = None
        if expected_partner_aic is not None or not identity_binding_enabled:
            self._client = self._build_notification_client(base, expected_partner_aic)
            self._rpc_client = self._build_rpc_client(base, expected_partner_aic)

 # task_id → token（用于 _validate_token）
        self._task_to_token: dict[str, str] = {}
 # task_id → session_id（用于回调路由）
        self._task_to_session: dict[str, str] = {}
 # task_id → Future[TaskResult]（终态回调时 resolve）
        self._task_futures: dict[str, asyncio.Future] = {}
 # 可选的外部回调 handler
        self._callback_handler: TaskResultCallback | None = None
 # fire-and-forget 任务引用集（防 GC 回收）
        self._bg_tasks: set[asyncio.Task[Any]] = set

    def _build_notification_client(
        self,
        partner_base_url: str,
        expected_partner_aic: str | None,
    ) -> AipNotificationClient:
        base = partner_base_url.rstrip("/")
        return AipNotificationClient(
            partner_url=base,
            leader_id=self._leader_id,
            ssl_context=self._ssl_context,
            transport=self._transport,
            expected_partner_aic=expected_partner_aic,
            identity_binding_enabled=self._identity_binding_enabled,
        )

    def _build_rpc_client(
        self,
        partner_base_url: str,
        expected_partner_aic: str | None,
    ) -> AipRpcClient:
        base = partner_base_url.rstrip("/")
        return AipRpcClient(
            partner_url=f"{base}/rpc",
            leader_id=self._leader_id,
            ssl_context=self._ssl_context,
            transport=self._transport,
            expected_partner_aic=expected_partner_aic,
            identity_binding_enabled=self._identity_binding_enabled,
        )

 # ------------------------------------------------------------------
 # Future-based waiting
 # ------------------------------------------------------------------

    def register_task_future(self, task_id: str) -> asyncio.Future[TaskResult]:
 """注册并返回一个 Future，当该 task 收到终态回调时自动 resolve。

        用于 AipExecutor 在 notification 模式下 await 终态结果。
 """
        loop = asyncio.get_event_loop
        future: asyncio.Future[TaskResult] = loop.create_future
        self._task_futures[task_id] = future
        return future

    def cancel_task_future(self, task_id: str) -> None:
 """取消并移除 task 的 Future（任务超时/中断时调用）。"""
        fut = self._task_futures.pop(task_id, None)
        if fut and not fut.done:
            fut.cancel

 # ------------------------------------------------------------------
 # callback handler
 # ------------------------------------------------------------------

    def set_callback_handler(self, handler: TaskResultCallback) -> None:
 """注册回调处理函数（收到 notification 后调用）。"""
        self._callback_handler = handler

 # ------------------------------------------------------------------
 # start（使用构造时的固定 partner URL）
 # ------------------------------------------------------------------

    async def start(
        self,
        session_id: str,
        user_input: str,
        task_id: str,
        *,
        notify_on_states: list[TaskState] | None = None,
        token: str | None = None,
    ) -> TaskResult:
 """启动任务并注册通知订阅（使用构造时指定的 partner URL）。

        Returns:
            来自 RPC start_task 的初始 TaskResult
 """
        if self._client is None or self._rpc_client is None:
            raise ValueError("expected_partner_aic is required for fixed-partner notification execution")
        return await self._start_with_clients(
            notification_client=self._client,
            rpc_client=self._rpc_client,
            session_id=session_id,
            user_input=user_input,
            task_id=task_id,
            notify_on_states=notify_on_states,
            token=token,
        )

    async def start_for_partner(
        self,
        partner_base_url: str,
        partner_aic: str,
        session_id: str,
        user_input: str,
        task_id: str,
        *,
        notify_on_states: list[TaskState] | None = None,
        token: str | None = None,
    ) -> TaskResult:
 """为指定 partner URL 启动任务并注册通知订阅。

        与 `start` 不同，此方法按调用时提供的 partner_base_url 创建临时 HTTP 客户端，
        适用于多 Partner 场景（每个 Partner 有不同的 URL）。

        Returns:
            来自 RPC start_task 的初始 TaskResult
 """
        notification_client = self._build_notification_client(partner_base_url, partner_aic)
        rpc_client = self._build_rpc_client(partner_base_url, partner_aic)
        try:
            return await self._start_with_clients(
                notification_client=notification_client,
                rpc_client=rpc_client,
                session_id=session_id,
                user_input=user_input,
                task_id=task_id,
                notify_on_states=notify_on_states,
                token=token,
            )
        finally:
            await notification_client.close
            await rpc_client.close

    async def _start_with_clients(
        self,
        notification_client: AipNotificationClient,
        rpc_client: AipRpcClient,
        session_id: str,
        user_input: str,
        task_id: str,
        *,
        notify_on_states: list[TaskState] | None = None,
        token: str | None = None,
    ) -> TaskResult:
 """内部：用指定 client 实例完成 start 流程。"""
        if token is None:
            token = secrets.token_hex(32)

        self._task_to_token[task_id] = token
        self._task_to_session[task_id] = session_id

        callback_url = f"{self._callback_base_url}/{task_id}"

 # 1. 注册通知配置
        config = await notification_client.set_notification(
            task_id=task_id,
            callback_url=callback_url,
            token=token,
        )

 # 2. RPC 启动任务（获取初始 TaskResult）
        initial_result = await rpc_client.start_task(
            session_id=session_id,
            user_input=user_input,
            task_id=task_id,
        )

 # 3. 注册通知订阅（告知 Partner：当此 task 状态变化时推送回调）
        if config.id:
            await notification_client.start_notification(
                task_id=task_id,
                config_id=config.id,
                session_id=session_id,
                notify_on_states=[s.value for s in notify_on_states] if notify_on_states else None,
            )

 # 若 Partner 在 RPC start_task 返回前已进入终态，则真实 notification 可能早于
 # notification/start 订阅建立而丢失。此时以已完成身份校验的 RPC 结果作为
 # 回填，复用同一分发路径，保证等待 future / callback_handler 都能收敛。
        if initial_result.status.state in _TERMINAL_STATES:
            await self._dispatch_callback(initial_result)

        return initial_result

 # ------------------------------------------------------------------
 # callback routing
 # ------------------------------------------------------------------

    def _validate_token(self, token: str, task_result: TaskResult) -> bool:
 """时序安全 token 校验（防时序攻击）。"""
        expected = self._task_to_token.get(task_result.taskId)
        if not expected:
            return False
        return secrets.compare_digest(expected.encode, token.encode)

    async def on_callback(self, task_result: TaskResult) -> None:
 """收到 Partner 推送时的处理入口。"""
        _t = asyncio.create_task(self._dispatch_callback(task_result))
        self._bg_tasks.add(_t)
        _t.add_done_callback(self._bg_tasks.discard)

    async def _dispatch_callback(self, task_result: TaskResult) -> None:
 """将 TaskResult 分发给注册的 callback_handler，并 resolve 对应 task Future。"""
        task_id = task_result.taskId

 # 若有终态 future，在终态时 resolve（供 AipExecutor.wait 使用）
        if task_id in self._task_futures and task_result.status.state in _TERMINAL_STATES:
            fut = self._task_futures.pop(task_id, None)
            if fut and not fut.done:
                fut.set_result(task_result)

        if self._callback_handler is not None:
            try:
                await self._callback_handler(task_result)
            except Exception as exc:
                logger.warning(
                    "NotificationExecutor: callback handler raised: task_id=%s error=%s",
                    task_result.taskId,
                    str(exc),
                )

    def build_receiver(self) -> NotificationReceiver:
 """构建 NotificationReceiver（挂载到 Leader FastAPI 应用）。

        使用 token_validator 实现多任务场景下的按 task_id 校验：
        SDK 的 NotificationReceiver 先解析 TaskResult，再调用 token_validator，
        因此可在此处比对 _task_to_token 映射中该任务的预期 token。
 """
        executor = self

        async def _handler(task_result: TaskResult) -> None:
            await executor.on_callback(task_result)

        return NotificationReceiver(
            token="",  # token_validator 优先，此字段不生效
            handler=_handler,
            token_validator=executor._validate_token,
            identity_binding_enabled=self._identity_binding_enabled,
        )

    async def close(self) -> None:
 """释放所有内部 HTTP 客户端资源。"""
 # 清理所有未决 futures
        for task_id in list(self._task_futures):
            self.cancel_task_future(task_id)
        try:
            if self._client is not None:
                await self._client.close
        except Exception as exc:
            logger.warning("NotificationExecutor: error closing notification client: %s", str(exc))
        try:
            if self._rpc_client is not None:
                await self._rpc_client.close
        except Exception as exc:
            logger.warning("NotificationExecutor: error closing rpc client: %s", str(exc))
