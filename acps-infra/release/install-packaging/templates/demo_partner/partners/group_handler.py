"""
Partner 端群组模式处理器

本模块提供 Partner 端群组模式的支持：
1. 管理群组连接生命周期（加入/退出）
2. 接收群组任务命令并分发给 GenericRunner 处理
3. 发送任务状态更新到群组

群组通信流程：
- Leader 通过 RPC 发送群组邀请 (RabbitMQRequest)
- Partner 接受邀请，连接 RabbitMQ，加入群组
- Leader 通过群组广播发送任务命令
- Partner 通过群组广播发送任务状态更新
"""

import asyncio
import contextlib
import os
import ssl
from typing import TYPE_CHECKING, Any

import structlog
from acps_sdk.aip.aip_base_model import (
    TaskCommand,
    TaskCommandType,
    TaskResult,
)
from acps_sdk.aip.aip_group_model import (
    GroupMgmtCommand,
    InboxGroupInvitation,
    RabbitMQRequest,
    RabbitMQResponse,
)
from acps_sdk.aip.aip_group_partner import (
    GroupPartnerMqClient,
)
from aiormq.exceptions import AMQPError

if TYPE_CHECKING:
    from partners.generic_runner import GenericRunner


logger = structlog.get_logger

SHARED_INBOX_RETRY_SECONDS = 5
_NON_RETRYABLE_SHARED_INBOX_ERROR_MARKERS = (
    "ACCESS_REFUSED",
    "AUTHENTICATION",
    "LOGIN WAS REFUSED",
    "NOT_ALLOWED",
)


def _require_non_empty(value: str | None, field_name: str) -> str:
 """确保 RPC 响应中的关键字符串字段存在。"""
    if not value:
        raise ValueError(f"Missing required field: {field_name}")
    return value


class GroupHandler:
 """
    群组模式处理器

    管理 Partner 的群组连接和消息处理。
    每个 Partner Agent 可以同时参与多个群组（一个群组对应一个 session）。
 """

    def __init__(
        self,
        agent_name: str,
        runner: GenericRunner,
        rabbitmq_config: dict[str, Any] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        identity_binding_enabled: bool = True,
    ):
 """
        初始化群组处理器

        Args:
            agent_name: Partner 名称（用于构造 AIC）
            runner: 对应的 GenericRunner 实例（用于处理任务）
 """
        self.agent_name = agent_name
        self.runner = runner
        self.rabbitmq_config = rabbitmq_config or {}
        self.ssl_context = ssl_context
        self.identity_binding_enabled = identity_binding_enabled

 # AIC 标识（从 runner 的 ACS 获取或构造）
        acs = runner.acs
        self.partner_aic = acs.get("aic") or f"agent.{agent_name}"

 # 群组客户端缓存
 # group_id -> GroupPartnerMqClient
        self._group_clients: dict[str, GroupPartnerMqClient] = {}
        self._shared_mq_client: GroupPartnerMqClient | None = None
        self._shared_mq_retry_task: asyncio.Task[None] | None = None

 # 任务到群组的映射
 # task_id -> group_id
        self._task_group_map: dict[str, str] = {}

 # 设置状态变化回调，用于广播状态更新到群组
        self.runner.set_state_change_callback(self._on_runner_state_change)

        short_aic = self.partner_aic[-12:] if len(self.partner_aic) > 12 else self.partner_aic
        logger.info("Initialized", agent=agent_name, aic_suffix=short_aic)

    async def start(self) -> None:
        if self._shared_mq_client is not None:
            return
        if self._shared_mq_retry_task and not self._shared_mq_retry_task.done:
            return

        start_result = await self._start_shared_inbox_consumer
        if start_result is not False:
            return

        self._shared_mq_retry_task = asyncio.create_task(self._retry_start_shared_inbox_consumer)

    def _should_retry_shared_inbox_start(self, exc: Exception) -> bool:
        error_text = str(exc).upper
        if any(marker in error_text for marker in _NON_RETRYABLE_SHARED_INBOX_ERROR_MARKERS):
            return False
        if isinstance(exc, OSError):
            return True
        return True

    async def _start_shared_inbox_consumer(self) -> bool | None:
        mq_config = self._resolve_rabbitmq_config
        shared_mq_client = GroupPartnerMqClient(
            partner_aic=self.partner_aic,
            rabbitmq_host=mq_config["host"],
            rabbitmq_port=mq_config["port"],
            rabbitmq_vhost=mq_config["vhost"],
            rabbitmq_user=mq_config["user"],
            rabbitmq_password=mq_config["password"],
            ssl_context=self.ssl_context,
            identity_binding_enabled=self.identity_binding_enabled,
            robust_connection=True,
        )

        try:
            await shared_mq_client.connect
            await shared_mq_client.start_inbox_consuming(self._handle_inbox_invitation)
        except (AMQPError, OSError) as exc:
            with contextlib.suppress(Exception):
                await shared_mq_client.close
            should_retry = self._should_retry_shared_inbox_start(exc)
            logger.warning(
                "RabbitMQ unavailable, group inbox disabled",
                agent=self.agent_name,
                host=mq_config["host"],
                port=mq_config["port"],
                vhost=mq_config["vhost"],
                error=str(exc)[:100],
                will_retry=should_retry,
                retry_in_seconds=SHARED_INBOX_RETRY_SECONDS if should_retry else 0,
            )
            return False if should_retry else None

        self._shared_mq_client = shared_mq_client
        logger.info(
            "Inbox consumer started",
            agent=self.agent_name,
            host=mq_config["host"],
            port=mq_config["port"],
            vhost=mq_config["vhost"],
        )
        return True

    async def _retry_start_shared_inbox_consumer(self) -> None:
        try:
            while self._shared_mq_client is None:
                await asyncio.sleep(SHARED_INBOX_RETRY_SECONDS)
                start_result = await self._start_shared_inbox_consumer
                if start_result is True:
                    logger.info(
                        "Inbox consumer recovered",
                        agent=self.agent_name,
                    )
                    return
                if start_result is None:
                    return
        finally:
            self._shared_mq_retry_task = None

    def _resolve_rabbitmq_config(self) -> dict[str, Any]:
        config = dict(self.rabbitmq_config)

        host = os.getenv("RABBITMQ_HOST", config.get("host", "localhost"))
        port_raw = os.getenv("RABBITMQ_PORT", str(config.get("port", 5671)))
        try:
            port = int(port_raw)
        except ValueError:
            port = 5671

        vhost = os.getenv("RABBITMQ_VHOST", config.get("vhost", "acps"))
        user = config.get("user")
        password = config.get("password")

        if port != 5671 or self.ssl_context is None:
            env_user = os.getenv("RABBITMQ_USER", "")
            env_password = os.getenv("RABBITMQ_PASSWORD", "")
            user = env_user.strip or user
            password = env_password.strip or password

        return {
            "host": host,
            "port": port,
            "vhost": vhost,
            "user": user,
            "password": password,
        }

    def _create_group_client(self, *, use_shared_connection: bool) -> GroupPartnerMqClient:
        mq_config = self._resolve_rabbitmq_config
        shared_connection = (
            self._shared_mq_client.connection if use_shared_connection and self._shared_mq_client else None
        )
        return GroupPartnerMqClient(
            partner_aic=self.partner_aic,
            rabbitmq_host=mq_config["host"],
            rabbitmq_port=mq_config["port"],
            rabbitmq_vhost=mq_config["vhost"],
            rabbitmq_user=mq_config["user"],
            rabbitmq_password=mq_config["password"],
            ssl_context=self.ssl_context,
            connection=shared_connection,
            connection_owner=shared_connection is None,
            robust_connection=False,
            identity_binding_enabled=self.identity_binding_enabled,
            message_emitter=self.runner._message_emitter,
            message_system="rabbitmq",
 # trace_context_provider=None — O-M6 ContextVar 自动承接
        )

    def _bind_group_client(self, client: GroupPartnerMqClient) -> None:
        client.set_command_handler(self._on_task_command)
        client.set_task_result_handler(self._on_task_result)
        client.set_mgmt_command_handler(self._on_mgmt_command)
        client.set_disconnect_handler(self._on_group_client_disconnected)

    def _remove_group_state(
        self,
        group_id: str,
        client: GroupPartnerMqClient | None = None,
    ) -> int:
        current_client = self._group_clients.get(group_id)
        if client is not None and current_client not in (None, client):
            return 0

        self._group_clients.pop(group_id, None)
        tasks_to_remove = [
            task_id for task_id, mapped_group_id in self._task_group_map.items if mapped_group_id == group_id
        ]
        for task_id in tasks_to_remove:
            self._task_group_map.pop(task_id, None)
        return len(tasks_to_remove)

    async def _discard_group_client(
        self,
        group_id: str,
        client: GroupPartnerMqClient,
        *,
        reason: str,
    ) -> None:
        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        try:
            await client.close
        except Exception as exc:
            logger.warning(
                "Failed to close stale group client",
                agent=self.agent_name,
                group_suffix=short_group,
                reason=reason,
                error=str(exc)[:100],
            )
        finally:
            removed_task_count = self._remove_group_state(group_id, client)
            logger.info(
                "Discarded stale group client",
                agent=self.agent_name,
                group_suffix=short_group,
                reason=reason,
                cleaned_task_mappings=removed_task_count,
            )

    def _on_group_client_disconnected(
        self,
        client: GroupPartnerMqClient,
        group_id: str | None,
    ) -> None:
        if not group_id:
            return

        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        removed_task_count = self._remove_group_state(group_id, client)
        logger.info(
            "Group client disconnected",
            agent=self.agent_name,
            group_suffix=short_group,
            cleaned_task_mappings=removed_task_count,
        )

    async def _handle_inbox_invitation(self, invitation: InboxGroupInvitation) -> None:
        group_id = invitation.group.groupId
        short_group = group_id[-8:] if len(group_id) > 8 else group_id

        existing_client = self._group_clients.get(group_id)
        if existing_client and existing_client.is_joined:
            logger.info(
                "Inbox invitation ignored for existing group",
                agent=self.agent_name,
                group_suffix=short_group,
            )
            return
        if existing_client:
            await self._discard_group_client(
                group_id,
                existing_client,
                reason="inbox_reinvite",
            )

        client = self._create_group_client(use_shared_connection=False)
        self._bind_group_client(client)

        joined = await client.join_group_from_invitation(invitation)
        if joined:
            self._group_clients[group_id] = client
            logger.info(
                "Joined group from inbox",
                agent=self.agent_name,
                group_suffix=short_group,
            )

    async def _on_runner_state_change(self, task_result: TaskResult) -> None:
 """
        GenericRunner 状态变化回调

        当任务状态变化时，广播到对应的群组
 """
        task_id = task_result.taskId
        if not task_id:
            return

        short_task = task_id[-12:] if len(task_id) > 12 else task_id
        state_name = (
            task_result.status.state.name
            if task_result.status and hasattr(task_result.status.state, "name")
            else str(task_result.status.state)
            if task_result.status
            else "unknown"
        )

 # 查找任务对应的群组
        group_id = self._task_group_map.get(task_id)
        if not group_id:
            logger.debug(
                "State change for task but no group mapping, skipping",
                agent=self.agent_name,
                task_suffix=short_task,
            )
            return

        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        logger.info(
            "State change callback: broadcasting to group",
            agent=self.agent_name,
            task_suffix=short_task,
            state=state_name,
            group_suffix=short_group,
        )

        await self._broadcast_task_update(task_result, group_id)

    @property
    def active_groups(self) -> dict[str, GroupPartnerMqClient]:
 """获取活跃的群组连接"""
        return {gid: client for gid, client in self._group_clients.items if client.is_joined}

    async def handle_group_rpc(self, request: RabbitMQRequest) -> RabbitMQResponse:
 """
        处理群组相关的 RPC 请求（joinGroup）

        RabbitMQRequest 的 method 字段固定为 "group"，
        实际代表 joinGroup 请求。

        Args:
            request: RabbitMQ 请求

        Returns:
            RabbitMQ 响应
 """
        method = request.method
        request_id = request.id or "unknown"
        logger.info(
            "Group RPC request",
            agent=self.agent_name,
            method=method,
            request_id=request_id,
        )

 # RabbitMQRequest.method 固定为 "group"，代表 joinGroup 请求
        if method == "group":
            return await self._handle_join_group(request)
        logger.warning("Unknown method", agent=self.agent_name, method=method)
        from acps_sdk.aip.aip_group_model import RabbitMQResponseError

        return RabbitMQResponse(
            id=request.id,
            error=RabbitMQResponseError(
                code=-32601,
                message=f"Method not found: {method}",
            ),
        )

    async def _handle_join_group(self, request: RabbitMQRequest) -> RabbitMQResponse:
 """
        处理群组加入请求

        Args:
            request: RabbitMQ 请求（已经是正确类型，无需转换）

        Returns:
            RabbitMQ 响应
 """
        from acps_sdk.aip.aip_group_model import (
            RabbitMQResponseError,
            RabbitMQResponseResult,
        )

        start_time = asyncio.get_event_loop.time

        try:
 # request 已经是 RabbitMQRequest 类型，直接使用
            rabbitmq_request = request

            group_id = rabbitmq_request.params.group.groupId
            short_group = group_id[-8:] if len(group_id) > 8 else group_id
 # GroupInfo.leader 是 ACSObject 类型，包含 aic 字段
            leader_aic = rabbitmq_request.params.group.leader.aic if rabbitmq_request.params.group.leader else "unknown"
            short_leader = leader_aic[-8:] if len(leader_aic) > 8 else leader_aic

            logger.info(
                "joinGroup request",
                agent=self.agent_name,
                group_suffix=short_group,
                leader_suffix=short_leader,
            )

            server_params = rabbitmq_request.params.server
            amqp_params = rabbitmq_request.params.amqp
            logger.debug(
                "RabbitMQ config",
                agent=self.agent_name,
                host=server_params.host,
                port=server_params.port,
                exchange=amqp_params.exchange,
            )

 # 检查是否已经加入该群组
            if group_id in self._group_clients:
                existing_client = self._group_clients[group_id]
                if existing_client.is_joined:
                    logger.warning(
                        "Already joined group, returning existing info",
                        agent=self.agent_name,
                        group_suffix=short_group,
                    )
 # 返回现有连接信息
                    return RabbitMQResponse(
                        id=request.id,
                        result=RabbitMQResponseResult(
                            connectionName=_require_non_empty(existing_client._connection_name, "connectionName"),
                            vhost=_require_non_empty(existing_client._vhost, "vhost"),
                            nodeName=_require_non_empty(existing_client._node_name, "nodeName"),
                            queueName=_require_non_empty(existing_client.queue_name, "queueName"),
                            processId=f"pid-{os.getpid}",
                        ),
                    )
 # 清理旧客户端
                logger.debug(
                    "Cleaning up disconnected client for group",
                    agent=self.agent_name,
                    group_suffix=short_group,
                )
                await self._discard_group_client(
                    group_id,
                    existing_client,
                    reason="rpc_rejoin",
                )

 # 创建新的群组客户端
            logger.debug("Creating GroupPartnerMqClient", agent=self.agent_name)
            client = self._create_group_client(use_shared_connection=False)
            self._bind_group_client(client)

 # 加入群组
            logger.debug("Joining group via MQ client", agent=self.agent_name)
            response = await client.join_group(rabbitmq_request)

            if response.error:
                elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
                logger.error(
                    "joinGroup FAILED",
                    agent=self.agent_name,
                    group_suffix=short_group,
                    error=response.error.message,
                    elapsed_ms=f"{elapsed_ms:.0f}",
                )
                return RabbitMQResponse(
                    id=request.id,
                    error=RabbitMQResponseError(
                        code=response.error.code,
                        message=response.error.message,
                    ),
                )

 # 保存客户端
            self._group_clients[group_id] = client

            elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
            logger.info(
                "joinGroup SUCCESS",
                agent=self.agent_name,
                group_suffix=short_group,
                queue=client.queue_name,
                elapsed_ms=f"{elapsed_ms:.0f}",
            )

 # 返回 RabbitMQ 响应（直接返回，无需转换）
            result = response.result
            if result is None:
                raise ValueError("join_group returned no result")

            return RabbitMQResponse(
                id=request.id,
                result=RabbitMQResponseResult(
                    connectionName=_require_non_empty(result.connectionName, "connectionName"),
                    vhost=_require_non_empty(result.vhost, "vhost"),
                    nodeName=_require_non_empty(result.nodeName, "nodeName"),
                    queueName=_require_non_empty(result.queueName, "queueName"),
                    processId=_require_non_empty(result.processId, "processId"),
                ),
            )

        except Exception as e:
            elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
            logger.exception(
                "joinGroup ERROR",
                agent=self.agent_name,
                error=str(e)[:100],
                elapsed_ms=f"{elapsed_ms:.0f}",
            )
            return RabbitMQResponse(
                id=request.id,
                error=RabbitMQResponseError(
                    code=-32603,
                    message=f"Internal error: {e!s}",
                ),
            )

    async def _on_task_command(self, command: TaskCommand, is_mentioned: bool) -> None:
 """
        处理来自群组的任务命令

        Args:
            command: 任务命令
            is_mentioned: 是否被提及（即是否需要处理）
 """
        task_id = command.taskId or command.id
        sender_id = command.senderId or ""
        short_task = task_id[-12:] if len(task_id) > 12 else task_id
        short_sender = sender_id[-8:] if len(sender_id) > 8 else sender_id

        logger.info(
            "TaskCommand received",
            agent=self.agent_name,
            cmd=(command.command.name if hasattr(command.command, "name") else command.command),
            task_suffix=short_task,
            sender_suffix=short_sender,
            mentioned=is_mentioned,
        )

 # 如果没有被提及，不处理
        if not is_mentioned:
            logger.debug(
                "Not mentioned, skipping command for task",
                agent=self.agent_name,
                task_suffix=short_task,
            )
            return

 # 记录任务到群组的映射
        group_id = self._resolve_group_for_task_command(command)
        short_group = group_id[-8:] if group_id and len(group_id) > 8 else group_id
        if group_id:
            self._task_group_map[task_id] = group_id
            logger.debug(
                "Mapped task to group",
                agent=self.agent_name,
                task_suffix=short_task,
                group_suffix=short_group,
            )

 # 获取现有任务（如果存在）
        task_ctx = self.runner.tasks.get(task_id)
        task = task_ctx.task if task_ctx else None

 # 将群组命令转换为 GenericRunner 的内部任务处理
 # 这里复用 GenericRunner 的现有逻辑
        start_time = asyncio.get_event_loop.time

        try:
            cmd_type = command.command

            if cmd_type == TaskCommandType.Start:
                logger.debug("Processing START command", agent=self.agent_name)
 # 创建任务
                task_result = await self.runner.on_start(command, task)
 # 发送状态更新到群组
                await self._broadcast_task_update(task_result, group_id)

            elif cmd_type == TaskCommandType.Continue:
                if not task:
                    logger.warning(
                        "Task not found for CONTINUE",
                        agent=self.agent_name,
                        task_suffix=short_task,
                    )
                    return
                logger.debug("Processing CONTINUE command", agent=self.agent_name)
 # 继续任务
                task_result = await self.runner.on_continue(command, task)
                await self._broadcast_task_update(task_result, group_id)

            elif cmd_type == TaskCommandType.Complete:
                if not task:
                    logger.warning(
                        "Task not found for COMPLETE",
                        agent=self.agent_name,
                        task_suffix=short_task,
                    )
                    return
                logger.debug("Processing COMPLETE command", agent=self.agent_name)
 # 完成任务
                task_result = await self.runner.on_complete(command, task)
                await self._broadcast_task_update(task_result, group_id)

            elif cmd_type == TaskCommandType.Cancel:
                if not task:
                    logger.warning(
                        "Task not found for CANCEL",
                        agent=self.agent_name,
                        task_suffix=short_task,
                    )
                    return
                logger.debug("Processing CANCEL command", agent=self.agent_name)
 # 取消任务
                task_result = await self.runner.on_cancel(command, task)
                await self._broadcast_task_update(task_result, group_id)

            elif cmd_type == TaskCommandType.Get:
                if not task:
                    logger.warning(
                        "Task not found for GET",
                        agent=self.agent_name,
                        task_suffix=short_task,
                    )
                    return
                logger.debug("Processing GET command", agent=self.agent_name)
 # 获取任务状态
                task_result = await self.runner.on_get(command, task)
                await self._broadcast_task_update(task_result, group_id)

            else:
                logger.warning("Unknown command type", agent=self.agent_name, cmd_type=cmd_type)

            elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
            logger.debug(
                "Command processed",
                agent=self.agent_name,
                task_suffix=short_task,
                elapsed_ms=f"{elapsed_ms:.0f}",
            )

        except Exception as e:
            elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
            logger.exception(
                "Error processing command",
                agent=self.agent_name,
                task_suffix=short_task,
                error=str(e)[:100],
                elapsed_ms=f"{elapsed_ms:.0f}",
            )

    async def _on_task_result(self, task_result: TaskResult) -> None:
 """
        处理来自其他 Partner 的任务结果

        Args:
            task_result: 任务结果
 """
 # 通常 Partner 不需要处理其他 Partner 的任务结果
 # 但可以用于观察群组内的活动
        short_task = task_result.id[-12:] if len(task_result.id) > 12 else task_result.id
        state_name = (
            task_result.status.state.name
            if task_result.status and hasattr(task_result.status.state, "name")
            else str(task_result.status.state)
            if task_result.status
            else "unknown"
        )
        logger.debug(
            "Received task result from other partner",
            agent=self.agent_name,
            task_suffix=short_task,
            state=state_name,
        )

    async def _on_mgmt_command(self, mgmt_cmd: GroupMgmtCommand) -> None:
 """
        处理群组管理命令

        Args:
            mgmt_cmd: 管理命令
 """
        short_sender = mgmt_cmd.senderId[-8:] if len(mgmt_cmd.senderId) > 8 else mgmt_cmd.senderId
        logger.info(
            "Received mgmt command",
            agent=self.agent_name,
            cmd=mgmt_cmd.command,
            sender_suffix=short_sender,
        )
 # 大部分管理命令由 GroupPartnerMqClient 内部处理
 # 这里可以添加额外的业务逻辑

    async def _broadcast_task_update(
        self,
        task_result: TaskResult,
        group_id: str | None = None,
    ) -> None:
 """
        广播任务状态更新到群组

        Args:
            task_result: 任务结果
            group_id: 群组 ID（如果为 None，尝试从任务映射查找）
 """
        short_task = task_result.id[-12:] if len(task_result.id) > 12 else task_result.id
        state_name = (
            task_result.status.state.name
            if task_result.status and hasattr(task_result.status.state, "name")
            else str(task_result.status.state)
            if task_result.status
            else "unknown"
        )

        task_id = task_result.taskId or task_result.id

        if not group_id:
            group_id = self._task_group_map.get(task_id)

        if not group_id:
            logger.warning(
                "Cannot find group for task, skipping broadcast",
                agent=self.agent_name,
                task_suffix=short_task,
            )
            return

        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        client = self._group_clients.get(group_id)
        if not client or not client.is_joined:
            logger.warning(
                "Not connected to group, skipping broadcast",
                agent=self.agent_name,
                group_suffix=short_group,
            )
            return

        try:
            logger.debug(
                "Broadcasting task update",
                agent=self.agent_name,
                task_suffix=short_task,
                state=state_name,
                group_suffix=short_group,
            )

 # 从 task_result 中提取必要的字段
            session_id = task_result.sessionId
            if not session_id:
                logger.warning(
                    "task_result.sessionId is None, skipping broadcast",
                    agent=self.agent_name,
                    task_suffix=short_task,
                )
                return
            if not task_result.status:
                logger.warning(
                    "task_result.status is None, skipping broadcast",
                    agent=self.agent_name,
                    task_suffix=short_task,
                )
                return
            state = task_result.status.state
            products = task_result.products
            status_data_items = task_result.status.dataItems

            await client.send_task_result(
                task_id=task_id,
                session_id=session_id,
                state=state,
                products=products,
                status_data_items=status_data_items,
            )

            logger.info(
                "Task update broadcasted",
                agent=self.agent_name,
                task_suffix=short_task,
                state=state_name,
            )
        except Exception as e:
            logger.error(
                "Failed to broadcast task update",
                agent=self.agent_name,
                task_suffix=short_task,
                error=str(e)[:100],
            )

    def _find_group_for_sender(self, sender_id: str) -> str | None:
 """
        根据发送者 ID 查找对应的群组

        Args:
            sender_id: 发送者 AIC

        Returns:
            群组 ID 或 None
 """
        short_sender = sender_id[-8:] if len(sender_id) > 8 else sender_id

 # 如果发送者是 Leader，查找其所属的群组
        for group_id, client in self._group_clients.items:
            if client.is_joined and client._group_info:
 # GroupInfo.leader 是 ACSObject 类型
                leader_aic = client._group_info.leader.aic if client._group_info.leader else None
                if leader_aic == sender_id:
                    short_group = group_id[-8:] if len(group_id) > 8 else group_id
                    logger.debug(
                        "Found group for sender",
                        agent=self.agent_name,
                        sender_suffix=short_sender,
                        group_suffix=short_group,
                    )
                    return group_id

        logger.debug(
            "No group found for sender",
            agent=self.agent_name,
            sender_suffix=short_sender,
        )
        return None

    def _resolve_group_for_task_command(self, command: TaskCommand) -> str | None:
 """为任务命令解析唯一的群组 ID。"""
        group_id = command.groupId
        if group_id:
            client = self._group_clients.get(group_id)
            if client and client.is_joined:
                short_group = group_id[-8:] if len(group_id) > 8 else group_id
                logger.debug(
                    "Resolved group from task command",
                    agent=self.agent_name,
                    group_suffix=short_group,
                )
                return group_id

        sender_id = command.senderId or ""
        return self._find_group_for_sender(sender_id) if sender_id else None

    async def leave_group(self, group_id: str) -> bool:
 """
        离开群组

        Args:
            group_id: 群组 ID

        Returns:
            是否成功离开
 """
        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        client = self._group_clients.get(group_id)
        if not client:
            logger.warning(
                "[GroupHandler:%s] Not in group ...%s",
                self.agent_name,
                short_group,
            )
            return False

        try:
            logger.info(
                "[GroupHandler:%s] Leaving group ...%s...",
                self.agent_name,
                short_group,
            )
            mapped_task_count = sum(
                1 for mapped_group_id in self._task_group_map.values if mapped_group_id == group_id
            )
            await client.leave_group
            self._remove_group_state(group_id, client)

            logger.info(
                "[GroupHandler:%s] Left group ...%s, cleaned %d task mappings",
                self.agent_name,
                short_group,
                mapped_task_count,
            )
            return True

        except Exception as e:
            logger.error(
                "[GroupHandler:%s] Failed to leave group ...%s: %s",
                self.agent_name,
                short_group,
                str(e)[:100],
            )
            return False

    async def leave_all_groups(self) -> None:
 """离开所有群组"""
        group_count = len(self._group_clients)
        if group_count == 0:
            logger.debug("[GroupHandler:%s] No groups to leave", self.agent_name)
            return

        logger.info("[GroupHandler:%s] Leaving %d groups...", self.agent_name, group_count)
        group_ids = list(self._group_clients.keys)
        for group_id in group_ids:
            await self.leave_group(group_id)
        logger.info("[GroupHandler:%s] Left all groups", self.agent_name)

    async def shutdown(self) -> None:
 """关闭群组处理器"""
        logger.info("[GroupHandler:%s] Shutting down...", self.agent_name)
        self.runner.remove_state_change_listener(self._on_runner_state_change)
        self.runner._on_state_change_callback = None
        if self._shared_mq_retry_task:
            self._shared_mq_retry_task.cancel
            with contextlib.suppress(asyncio.CancelledError):
                await self._shared_mq_retry_task
            self._shared_mq_retry_task = None
        await self.leave_all_groups
        if self._shared_mq_client:
            await self._shared_mq_client.close
            self._shared_mq_client = None
        logger.info("[GroupHandler:%s] Shutdown complete", self.agent_name)
