"""
群组模式 Partner 客户端模块

本模块实现了 AIP 协议中群组模式 (Group Mode) 下 Partner 角色的核心功能：
1. 接收 Leader 发送的群组邀请 (RabbitMQRequest)
2. 连接到 RabbitMQ 消息队列服务器
3. 创建自己的队列并绑定到群组 Exchange
4. 接收和处理群组内的消息（任务消息、管理消息）
5. 发送任务状态更新和产出物到群组
6. 响应群组管理命令（状态查询、退出、静音等）
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import ssl
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union, TYPE_CHECKING

import aio_pika
from aio_pika import ExchangeType
from aio_pika import Message as AMQPMessage
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)

from .aip_base_model import (
    DataItem,
    Product,
    TaskCommand,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from .aip_group_model import (
    AMQPConfig,
    GroupInfo,
    GroupInvitationError,
    GroupInvitationErrorData,
    GroupMemberStatus,
    GroupMgmtCommand,
    GroupMgmtCommandType,
    GroupMgmtResult,
    InboxGroupInvitation,
    InboxGroupInvitationError,
    RabbitMQRequest,
    RabbitMQResponse,
    RabbitMQResponseError,
    RabbitMQResponseErrorData,
    RabbitMQResponseResult,
    RabbitMQServerConfig,
)
from .aip_group_identity import (
    assert_incoming_group_message_identity,
    build_outgoing_amqp_user_id,
    extract_amqp_user_id,
)
from .aip_group_runtime import (
    DEFAULT_INVITATION_TIMEOUT_SECONDS,
    INBOX_EXCHANGE_NAME,
    INBOX_MESSAGE_TTL_MS,
    INBOX_QUEUE_EXPIRES_MS,
    build_external_connection_url,
    build_group_queue_name,
    build_inbox_queue_name,
    build_plain_connection_url,
    calculate_invitation_expiry,
    invitation_is_expired,
)

if TYPE_CHECKING:
    from acps_sdk.amp.message_emitter import MessageEmitter

from acps_sdk.amp import _message_tap as message_tap
from acps_sdk.amp.trace_context import (
    TRACEPARENT_HEADER,
    TraceContext,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)

logger = logging.getLogger("acps_sdk.aip.group_partner")

DATA_PLANE_TYPES = frozenset({"task-command", "task-result"})
_active_cmd_trace: ContextVar[TraceContext | None] = ContextVar(
    "amp_group_partner_cmd_trace", default=None
)


class PartnerGroupState(str, Enum):
    """Partner 群组连接状态"""

    DISCONNECTED = "disconnected"  # 未连接
    JOINED = "joined"  # 已加入
    LEAVING = "leaving"  # 正在退出


# 消息处理回调类型
# (command, is_mentioned) -> None
TaskCommandHandler = Callable[[TaskCommand, bool], Awaitable[None]]
TaskResultHandler = Callable[[TaskResult], Awaitable[None]]
MgmtCommandHandler = Callable[[GroupMgmtCommand], Awaitable[None]]
InvitationHandler = Callable[[InboxGroupInvitation], Awaitable[bool]]
DisconnectHandler = Callable[
    ["GroupPartnerMqClient", Optional[str]], Awaitable[None] | None
]


class GroupPartnerMqClient:
    """
    群组模式 Partner 客户端

    负责：
    - 接收群组邀请并连接到 RabbitMQ
    - 创建自己的队列并绑定到群组 Exchange
    - 接收和处理群组消息
    - 发送任务状态更新和产出物
    - 响应群组管理命令
    """

    def __init__(
        self,
        partner_aic: str,
        rabbitmq_host: str = "localhost",
        rabbitmq_port: int = 5672,
        rabbitmq_vhost: str = "/",
        rabbitmq_user: Optional[str] = "guest",
        rabbitmq_password: Optional[str] = "guest",
        ssl_context: Optional[ssl.SSLContext] = None,
        connection: Optional[AbstractConnection] = None,
        connection_owner: bool = True,
        robust_connection: bool = True,
        *,
        message_emitter: MessageEmitter | None = None,
        message_system: str = "rabbitmq",
        trace_context_provider: Callable[[], TraceContext | None] | None = None,
        identity_binding_enabled: bool = True,
    ):
        """
        初始化 Partner 客户端

        Args:
            partner_aic: Partner 的 AIC 标识
        """
        self.partner_aic = partner_aic
        self.rabbitmq_host = rabbitmq_host
        self.rabbitmq_port = rabbitmq_port
        self.rabbitmq_vhost = rabbitmq_vhost
        self.rabbitmq_user = rabbitmq_user or None
        self.rabbitmq_password = rabbitmq_password or None
        self.ssl_context = ssl_context
        self._connection_owner = connection_owner
        self._use_robust_connection = robust_connection
        self._message_emitter = message_emitter
        self._message_system = message_system
        self._trace_context_provider = trace_context_provider
        self._identity_binding_enabled = identity_binding_enabled
        if not self._identity_binding_enabled:
            logger.warning(
                "AIP identity binding disabled for GroupPartnerMqClient partner_aic=%s",
                self.partner_aic,
            )

        # 连接和通道
        self._connection: Optional[AbstractConnection] = connection
        self._channel: Optional[AbstractChannel] = None
        self._inbox_channel: Optional[AbstractChannel] = None
        self._inbox_queue: Optional[AbstractQueue] = None
        # 延迟退出标记：收到 LEAVE_GROUP 命令时不能立即断开（需等消息 ACK 完成），
        # 因此先标记 _pending_leave，在 on_message 回调结束后再执行实际退出。
        self._pending_leave: bool = False
        # 暂存退出命令中的 sessionId，供延迟执行的 _handle_leave_request 回显使用
        self._pending_leave_session_id: Optional[str] = None

        # 群组信息
        self._group_id: Optional[str] = None
        self._group_info: Optional[GroupInfo] = None
        self._exchange_name: Optional[str] = None
        self._exchange: Optional[AbstractExchange] = None
        self._queue: Optional[AbstractQueue] = None
        self._queue_name: Optional[str] = None

        # 连接信息（用于返回给 Leader）
        self._connection_name: Optional[str] = None
        self._vhost: Optional[str] = None
        self._node_name: Optional[str] = None

        # 状态
        self._state: PartnerGroupState = PartnerGroupState.DISCONNECTED
        # 静音状态。注意：当前实现中，暂时没有强制限制，依靠 Partner 业务逻辑自觉遵守。
        self._muted: bool = False

        # 消息处理器
        self._command_handler: Optional[TaskCommandHandler] = None
        self._task_result_handler: Optional[TaskResultHandler] = None
        self._mgmt_command_handler: Optional[MgmtCommandHandler] = None
        self._consumer_tag: Optional[str] = None
        self._invitation_handler: Optional[InvitationHandler] = None
        self._disconnect_handler: Optional[DisconnectHandler] = None

        # 任务跟踪
        # task_id -> TaskState
        self._task_states: Dict[str, TaskState] = {}

    @property
    def group_id(self) -> Optional[str]:
        return self._group_id

    @property
    def state(self) -> PartnerGroupState:
        return self._state

    @property
    def is_joined(self) -> bool:
        return (
            self._state == PartnerGroupState.JOINED
            and self._connection is not None
            and not self._connection.is_closed
        )

    @property
    def is_muted(self) -> bool:
        return self._muted

    @property
    def queue_name(self) -> Optional[str]:
        return self._queue_name

    @property
    def connection(self) -> Optional[AbstractConnection]:
        return self._connection

    async def connect(self) -> None:
        if self._connection and not getattr(self._connection, "is_closed", False):
            if self._channel is None or getattr(self._channel, "is_closed", False):
                self._channel = await self._connection.channel()
            return

        if self._connection and getattr(self._connection, "is_closed", False):
            if not self._connection_owner:
                raise RuntimeError(
                    "Shared connection is closed; owner must recreate it"
                )

        connection_name = f"agent-{self.partner_aic}"
        connection_kwargs: Dict[str, Any] = {}
        if self.rabbitmq_user and self.rabbitmq_password:
            connection_url = build_plain_connection_url(
                host=self.rabbitmq_host,
                port=self.rabbitmq_port,
                vhost=self.rabbitmq_vhost,
                username=self.rabbitmq_user,
                password=self.rabbitmq_password,
                connection_name=connection_name,
            )
        else:
            if self.ssl_context is None:
                raise RuntimeError(
                    "ssl_context is required for AMQPS + EXTERNAL authentication"
                )
            connection_url = build_external_connection_url(
                host=self.rabbitmq_host,
                port=self.rabbitmq_port,
                vhost=self.rabbitmq_vhost,
                connection_name=connection_name,
            )
            connection_kwargs["ssl_context"] = self.ssl_context

        connect = (
            aio_pika.connect_robust if self._use_robust_connection else aio_pika.connect
        )
        self._connection = await connect(
            connection_url,
            **connection_kwargs,
        )
        self._channel = await self._connection.channel()

    async def ensure_inbox_queue(self) -> AbstractQueue:
        await self.connect()
        if self._inbox_queue and not getattr(self._inbox_queue, "is_closed", False):
            logger.debug(
                "event=partner_inbox_queue_reuse partner_aic=%s queue=%s",
                self.partner_aic,
                getattr(self._inbox_queue, "name", None),
            )
            return self._inbox_queue

        inbox_name = build_inbox_queue_name(self.partner_aic)
        connection = self._connection
        if connection is None:
            raise RuntimeError("RabbitMQ connection is not ready")

        self._inbox_channel = await connection.channel()
        self._inbox_queue = await self._inbox_channel.declare_queue(
            inbox_name,
            durable=True,
            exclusive=False,
            auto_delete=False,
            arguments={
                "x-expires": INBOX_QUEUE_EXPIRES_MS,
                "x-message-ttl": INBOX_MESSAGE_TTL_MS,
            },
        )
        inbox_exchange = await self._inbox_channel.declare_exchange(
            name=INBOX_EXCHANGE_NAME,
            type=ExchangeType.TOPIC,
            durable=True,
            auto_delete=False,
        )
        await self._inbox_queue.bind(inbox_exchange, routing_key=inbox_name)
        logger.info(
            "event=partner_inbox_queue_ready partner_aic=%s queue=%s expires_ms=%s message_ttl_ms=%s",
            self.partner_aic,
            inbox_name,
            INBOX_QUEUE_EXPIRES_MS,
            INBOX_MESSAGE_TTL_MS,
        )
        return self._inbox_queue

    async def start_inbox_consuming(
        self, handler: Callable[[InboxGroupInvitation], Awaitable[None]]
    ) -> None:
        inbox_queue = await self.ensure_inbox_queue()
        logger.info(
            "event=partner_inbox_consumer_start partner_aic=%s queue=%s",
            self.partner_aic,
            getattr(inbox_queue, "name", None),
        )

        async def on_message(message: AbstractIncomingMessage) -> None:
            async with message.process():
                try:
                    body = json.loads(message.body.decode())
                except json.JSONDecodeError:
                    logger.warning(
                        "event=partner_inbox_invalid_json partner_aic=%s",
                        self.partner_aic,
                    )
                    return

                if body.get("type") != "group-invitation":
                    logger.debug(
                        "event=partner_inbox_ignored partner_aic=%s type=%s",
                        self.partner_aic,
                        body.get("type"),
                    )
                    return

                try:
                    invitation = InboxGroupInvitation.model_validate(body)
                    if self._identity_binding_enabled:
                        assert_incoming_group_message_identity(
                            invitation,
                            amqp_user_id=extract_amqp_user_id(message),
                            expected_group_id=invitation.groupId,
                        )
                except Exception as exc:
                    logger.warning(
                        "event=partner_inbox_invalid_message partner_aic=%s error=%s",
                        self.partner_aic,
                        str(exc),
                    )
                    return
                logger.info(
                    "event=partner_inbox_invitation_received partner_aic=%s group_id=%s leader_aic=%s",
                    self.partner_aic,
                    invitation.group.groupId,
                    invitation.group.leader.aic,
                )
                await handler(invitation)

        await inbox_queue.consume(on_message)

    def set_invitation_handler(self, handler: InvitationHandler) -> None:
        self._invitation_handler = handler
        logger.debug(
            "event=partner_invitation_handler_set partner_aic=%s", self.partner_aic
        )

    def set_disconnect_handler(self, handler: DisconnectHandler) -> None:
        self._disconnect_handler = handler

    def set_command_handler(self, handler: TaskCommandHandler) -> None:
        """
        设置任务命令处理回调（必须）。Leader发出的命令消息需要在此回调函数处理。

        用于处理所有群组任务命令。
        Args:
            handler: 回调函数，接收 (command, is_mentioned) 参数
                     - command: TaskCommand 对象
                     - is_mentioned: 是否被提及（即是否需要作为任务处理）
        """
        self._command_handler = handler

    def set_task_result_handler(self, handler: TaskResultHandler) -> None:
        """设置任务结果处理回调（可选）。其它Partner发出的任务状态更新通过此回调处理（通常不需要处理，避免混乱自己的状态机）。"""
        self._task_result_handler = handler

    def set_mgmt_command_handler(self, handler: MgmtCommandHandler) -> None:
        """设置管理命令处理回调（可选）。用于处理群组管理消息的额外操作。通用操作如静音、状态查询等已经内置处理。"""
        self._mgmt_command_handler = handler

    async def join_group(self, request: RabbitMQRequest) -> RabbitMQResponse:
        """
        处理群组加入邀请

        根据设计文档 6.2.2 的流程：
        1. 接收邀请
        2. 建立连接
        3. 验证 Exchange
        4. 创建队列
        5. 绑定 Exchange
        6. 开始监听
        7. 返回连接信息
        8. 通知加入（发送 GroupMgmtMessage）

        Args:
            request: Leader 发送的群组邀请请求

        Returns:
            RabbitMQResponse 包含连接信息或错误
        """
        logger.info(
            "event=partner_join_invite_received partner_aic=%s group_id=%s exchange=%s host=%s",
            self.partner_aic,
            getattr(request.params.group, "groupId", None),
            getattr(request.params.amqp, "exchange", None),
            getattr(request.params.server, "host", None),
        )

        params = request.params
        server = params.server
        amqp = params.amqp
        group = params.group

        try:
            synthetic_invitation = self._build_direct_rpc_invitation(request)
            if self._invitation_handler and not await self._invitation_handler(
                synthetic_invitation
            ):
                logger.warning(
                    "event=partner_invitation_rejected partner_aic=%s group_id=%s route=rpc",
                    self.partner_aic,
                    group.groupId,
                )
                return RabbitMQResponse(
                    jsonrpc="2.0",
                    id=request.id,
                    error=RabbitMQResponseError(
                        code=-32020,
                        message="Invitation rejected by partner policy",
                        data=GroupInvitationErrorData(
                            errorType="INVITATION_REJECTED",
                            details="Partner invitation handler rejected direct RPC invite",
                        ),
                    ),
                )

            self._prepare_connection_from_server(server)
            result = await self._join_group_common(group=group, amqp=amqp)
            logger.info(
                "event=partner_invitation_accepted partner_aic=%s group_id=%s route=rpc",
                self.partner_aic,
                group.groupId,
            )
            return RabbitMQResponse(
                jsonrpc="2.0",
                id=request.id,
                result=result,
            )

        except Exception as e:
            logger.error(
                "event=join_group_failed partner_aic=%s error=%s",
                self.partner_aic,
                str(e),
            )
            with contextlib.suppress(Exception):
                await self._cleanup_group_resources()
            self._state = PartnerGroupState.DISCONNECTED

            return RabbitMQResponse(
                jsonrpc="2.0",
                id=request.id,
                error=RabbitMQResponseError(
                    code=-32021,
                    message="Connection failed",
                    data=RabbitMQResponseErrorData(
                        errorType="CONNECTION_FAILED",
                        details={
                            "host": server.host,
                            "port": server.port,
                            "reason": str(e),
                        },
                    ),
                ),
            )

    async def join_group_from_invitation(
        self, invitation: InboxGroupInvitation
    ) -> bool:
        if invitation_is_expired(invitation.expiresAt):
            logger.warning(
                "event=group_invitation_expired partner_aic=%s group_id=%s expires_at=%s",
                self.partner_aic,
                invitation.group.groupId,
                invitation.expiresAt,
            )
            return False

        try:
            logger.info(
                "event=partner_invitation_processing partner_aic=%s group_id=%s route=inbox",
                self.partner_aic,
                invitation.group.groupId,
            )
            if self._invitation_handler and not await self._invitation_handler(
                invitation
            ):
                logger.warning(
                    "event=partner_invitation_rejected partner_aic=%s group_id=%s route=inbox",
                    self.partner_aic,
                    invitation.group.groupId,
                )
                await self.send_invitation_error(
                    invitation,
                    GroupInvitationError(
                        code=-32020,
                        message="Invitation rejected by partner policy",
                        data=GroupInvitationErrorData(
                            errorType="INVITATION_REJECTED",
                            details="Partner invitation handler rejected inbox invite",
                        ),
                    ),
                )
                return False

            await self._join_group_common(
                group=invitation.group,
                amqp=invitation.amqp,
            )
            logger.info(
                "event=partner_invitation_accepted partner_aic=%s group_id=%s route=inbox",
                self.partner_aic,
                invitation.group.groupId,
            )
            return True
        except Exception as exc:
            logger.error(
                "event=join_group_from_inbox_failed partner_aic=%s group_id=%s error=%s",
                self.partner_aic,
                invitation.group.groupId,
                str(exc),
            )
            with contextlib.suppress(Exception):
                await self._cleanup_group_resources()
            await self.send_invitation_error(
                invitation,
                GroupInvitationError(
                    code=-32021,
                    message="Connection failed",
                    data=GroupInvitationErrorData(
                        errorType="CONNECTION_FAILED",
                        details=str(exc),
                    ),
                ),
            )
            return False

    async def send_invitation_error(
        self, invitation: InboxGroupInvitation, error: GroupInvitationError
    ) -> None:
        await self.connect()
        if not self._connection:
            raise RuntimeError("Connection is not available")

        channel = self._inbox_channel
        if channel is None or getattr(channel, "is_closed", False):
            channel = await self._connection.channel()

        exchange = await channel.declare_exchange(
            name=INBOX_EXCHANGE_NAME,
            type=ExchangeType.TOPIC,
            durable=True,
            auto_delete=False,
        )
        leader_inbox = build_inbox_queue_name(invitation.group.leader.aic)
        payload = InboxGroupInvitationError(
            id=f"invite-err-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="partner",
            senderId=self.partner_aic,
            groupId=invitation.group.groupId,
            invitationToken=invitation.invitationToken,
            error=error,
        )
        await exchange.publish(
            AMQPMessage(
                body=json.dumps(
                    payload.model_dump(exclude_none=True), ensure_ascii=False
                ).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                user_id=build_outgoing_amqp_user_id(
                    payload,
                    local_aic=self.partner_aic,
                    identity_binding_enabled=self._identity_binding_enabled,
                ),
            ),
            routing_key=leader_inbox,
        )

    def _build_direct_rpc_invitation(
        self, request: RabbitMQRequest
    ) -> InboxGroupInvitation:
        return InboxGroupInvitation(
            id=f"invite-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=request.params.group.leader.aic,
            groupId=request.params.group.groupId,
            protocol=request.params.protocol,
            expiresAt=calculate_invitation_expiry(DEFAULT_INVITATION_TIMEOUT_SECONDS),
            invitationToken=f"rpc-{uuid.uuid4()}",
            group=request.params.group,
            amqp=request.params.amqp,
        )

    def _prepare_connection_from_server(self, server: RabbitMQServerConfig) -> None:
        access_token_present = "accessToken" in server.model_fields_set
        access_token = server.accessToken

        self.rabbitmq_host = server.host
        self.rabbitmq_port = server.port
        self.rabbitmq_vhost = server.vhost
        self._vhost = server.vhost

        if access_token_present and access_token == "":
            raise ValueError("Malformed accessToken: empty string")

        if access_token_present and access_token:
            if ":" not in access_token:
                raise ValueError("Malformed accessToken: expected user:password")
            if self._connection and not self._connection_owner:
                raise RuntimeError(
                    "Cannot use shared EXTERNAL connection with accessToken auth"
                )
            username, password = access_token.split(":", 1)
            self.rabbitmq_user = username
            self.rabbitmq_password = password
            return

        self.rabbitmq_user = None
        self.rabbitmq_password = None
        if self.ssl_context is None:
            raise RuntimeError(
                "ssl_context is required when accessToken is missing or null"
            )

    async def _join_group_common(
        self,
        *,
        group: GroupInfo,
        amqp: AMQPConfig,
    ) -> RabbitMQResponseResult:
        self._group_id = group.groupId
        self._group_info = group
        self._exchange_name = amqp.exchange
        self._connection_name = f"agent-{self.partner_aic}"
        self._node_name = "unknown"
        self._vhost = self.rabbitmq_vhost

        await self.connect()
        channel = self._channel
        if channel is None:
            raise RuntimeError("RabbitMQ channel is not ready")

        try:
            self._exchange = await channel.get_exchange(
                self._exchange_name,
                ensure=False,
            )
        except Exception:
            self._exchange = await channel.declare_exchange(
                name=self._exchange_name,
                type=ExchangeType.FANOUT,
                durable=True,
                auto_delete=True,
                passive=True,
            )

        leader_aic = group.leader.aic if group.leader else "unknown"
        self._queue_name = build_group_queue_name(
            leader_aic, group.groupId, self.partner_aic
        )
        self._queue = await channel.declare_queue(
            name=self._queue_name,
            durable=True,
            exclusive=False,
            auto_delete=True,
        )
        await self._queue.bind(self._exchange)

        logger.info(
            "event=queue_created partner_aic=%s queue=%s exchange=%s",
            self.partner_aic,
            self._queue_name,
            self._exchange_name,
        )

        await self._start_consuming()
        self._state = PartnerGroupState.JOINED
        await self._notify_joined()

        return RabbitMQResponseResult(
            connectionName=self._connection_name,
            vhost=self._vhost or self.rabbitmq_vhost,
            nodeName=self._node_name,
            queueName=self._queue_name,
            processId=f"pid-{os.getpid()}",
        )

    async def _start_consuming(self) -> None:
        """开始消费消息"""
        if not self._queue:
            raise RuntimeError("Queue not created yet")

        async def on_message(message: AbstractIncomingMessage) -> None:
            t0 = monotonic()
            recv_span: str | None = None
            trace_id: str | None = None
            body: Dict[str, Any] = {}
            emit_ok = False

            async with message.process():
                try:
                    body = json.loads(message.body.decode())
                    if self._identity_binding_enabled:
                        assert_incoming_group_message_identity(
                            body,
                            amqp_user_id=extract_amqp_user_id(message),
                            expected_group_id=self._group_id,
                        )
                    logger.info(
                        "event=queue_message_received partner_aic=%s msg_type=%s sender=%s",
                        self.partner_aic,
                        body.get("type"),
                        body.get("senderId"),
                    )
                    emit_ok = bool(
                        self._message_emitter
                        and body.get("type") == "task-command"
                        and body.get("senderId") != self.partner_aic
                    )
                    if emit_ok:
                        ctx = parse_traceparent(
                            message.headers.get(TRACEPARENT_HEADER) if message.headers else None
                        )
                        trace_id = ctx.trace_id if ctx else new_trace_id()
                        recv_span = new_span_id()
                        await self._safe_emit(
                            message_tap.build_receive_body(
                                system=self._message_system,
                                exchange=self._exchange_name or "",
                                virtual_host=self.rabbitmq_vhost,
                                routing_key=message.routing_key or "",
                                queue_name=self._queue_name or "",
                                message_id=body.get("id", ""),
                                payload_size_bytes=len(message.body),
                                delivery_attempt=2 if message.redelivered else 1,
                                attributes={
                                    "groupId": self._group_id,
                                    "msgType": "task-command",
                                    "senderId": body.get("senderId"),
                                },
                            ),
                            trace_id=trace_id,
                            span_id=recv_span,
                            parent_span_id=ctx.span_id if ctx else "",
                            correlation_id=body.get("sessionId"),
                        )
                        _active_cmd_trace.set(TraceContext(trace_id=trace_id, span_id=recv_span))
                    await self._handle_incoming_message(body)
                except Exception as e:
                    if emit_ok and trace_id and recv_span:
                        await self._safe_emit(
                            message_tap.build_settlement_body(
                                "nack",
                                system=self._message_system,
                                exchange=self._exchange_name or "",
                                virtual_host=self.rabbitmq_vhost,
                                queue_name=self._queue_name or "",
                                message_id=body.get("id", ""),
                                delivery_attempt=2 if message.redelivered else 1,
                                latency_ms=(monotonic() - t0) * 1000,
                                reason=str(e)[:200],
                            ),
                            trace_id=trace_id,
                            span_id=new_span_id(),
                            parent_span_id=recv_span,
                            correlation_id=body.get("sessionId"),
                        )
                        _active_cmd_trace.set(None)
                    logger.error(
                        "event=message_processing_error partner_aic=%s error=%s",
                        self.partner_aic,
                        str(e),
                    )
                    raise
                if emit_ok and trace_id and recv_span:
                    await self._safe_emit(
                        message_tap.build_settlement_body(
                            "ack",
                            system=self._message_system,
                            exchange=self._exchange_name or "",
                            virtual_host=self.rabbitmq_vhost,
                            queue_name=self._queue_name or "",
                            message_id=body.get("id", ""),
                            delivery_attempt=2 if message.redelivered else 1,
                            latency_ms=(monotonic() - t0) * 1000,
                        ),
                        trace_id=trace_id,
                        span_id=new_span_id(),
                        parent_span_id=recv_span,
                        correlation_id=body.get("sessionId"),
                    )
                    _active_cmd_trace.set(None)
            if self._pending_leave:
                self._pending_leave = False
                await self._handle_leave_request()

        self._consumer_tag = await self._queue.consume(on_message)
        logger.info(
            "event=consuming_started partner_aic=%s group_id=%s",
            self.partner_aic,
            self._group_id,
        )

    async def _safe_emit(self, body: Any, **trace_kwargs: str | None) -> None:
        if not self._message_emitter:
            return
        try:
            await self._message_emitter.emit(body, **trace_kwargs)
        except Exception:
            logger.warning("message emit failed", exc_info=True)

    async def _handle_incoming_message(self, body: Dict[str, Any]) -> None:
        """处理接收到的消息"""
        msg_type = body.get("type")
        sender_id = body.get("senderId")

        # 忽略自己发送的消息
        if sender_id == self.partner_aic:
            return

        try:
            if msg_type == "group-mgmt-command":
                mgmt_cmd = GroupMgmtCommand.model_validate(body)
                logger.info(
                    "event=mgmt_command_received partner_aic=%s command=%s sender=%s",
                    self.partner_aic,
                    mgmt_cmd.command,
                    mgmt_cmd.senderId,
                )
                await self._handle_mgmt_command(mgmt_cmd)
            elif msg_type == "task-result":
                task_result = TaskResult.model_validate(body)
                if self._task_result_handler:
                    await self._task_result_handler(task_result)
            elif msg_type == "task-command":
                command = TaskCommand.model_validate(body)
                logger.info(
                    "event=task_command_received partner_aic=%s command=%s sender=%s task_id=%s",
                    self.partner_aic,
                    command.command,
                    command.senderId,
                    command.taskId,
                )
                await self._handle_task_command(command)
            elif msg_type == "group-mgmt-result":
                # 状态广播会发给所有成员，Partner 本地无需再处理。
                return
            else:
                logger.warning(
                    "event=unknown_message_type partner_aic=%s type=%s",
                    self.partner_aic,
                    msg_type,
                )
        except Exception as e:
            logger.error(
                "event=message_parse_error partner_aic=%s error=%s body=%s",
                self.partner_aic,
                str(e),
                str(body)[:200],
            )
            raise

    async def _handle_mgmt_command(self, command: GroupMgmtCommand) -> None:
        """处理群组管理命令"""
        # 检查是否需要响应
        is_targeted = command.must_respond(self.partner_aic)
        # 回显收到的命令中的 sessionId，而非自行推导
        echo_session_id = command.sessionId

        if command.command == GroupMgmtCommandType.GET_STATUS:
            if is_targeted:
                await self._send_status_response(session_id=echo_session_id)
        elif command.command in (
            GroupMgmtCommandType.LEAVE_GROUP,
            GroupMgmtCommandType.DISBAND,
        ):
            if is_targeted:
                self._pending_leave = True  # 延后执行，避免在 ack 前关闭 channel
                self._pending_leave_session_id = echo_session_id
        elif command.command == GroupMgmtCommandType.MUTE:
            if is_targeted:
                self._muted = True
                await self._send_status_response(session_id=echo_session_id)
                logger.info(
                    "event=partner_muted partner_aic=%s group_id=%s",
                    self.partner_aic,
                    self._group_id,
                )
        elif command.command == GroupMgmtCommandType.UNMUTE:
            if is_targeted:
                self._muted = False
                await self._send_status_response(session_id=echo_session_id)
                logger.info(
                    "event=partner_unmuted partner_aic=%s group_id=%s",
                    self.partner_aic,
                    self._group_id,
                )

        # 调用自定义处理器
        if self._mgmt_command_handler:
            await self._mgmt_command_handler(command)

    async def _handle_task_command(self, command: TaskCommand) -> None:
        """处理任务命令"""
        # 检查是否被提及
        mentions = command.mentions
        is_mentioned = False

        if mentions is None or (isinstance(mentions, list) and len(mentions) == 0):
            # 未指定 mentions，所有 Partner 都可以响应
            is_mentioned = True
        elif mentions == "all":
            is_mentioned = True
        elif isinstance(mentions, list) and self.partner_aic in mentions:
            is_mentioned = True

        # 统一调用命令处理器
        if self._command_handler:
            await self._command_handler(command, is_mentioned)

    async def _notify_joined(self) -> None:
        """通知群组自己已加入（加入通知时尚未有会话上下文，不传 sessionId）"""
        await self.send_status_result(connected=True, muted=False)
        logger.info(
            "event=partner_join_notified partner_aic=%s group_id=%s queue=%s",
            self.partner_aic,
            self._group_id,
            self._queue_name,
        )

    async def _send_status_response(self, session_id: Optional[str] = None) -> None:
        """发送状态响应"""
        await self.send_status_result(
            connected=self._state == PartnerGroupState.JOINED,
            muted=self._muted,
            session_id=session_id,
        )
        logger.info(
            "event=partner_status_response partner_aic=%s connected=%s muted=%s",
            self.partner_aic,
            self._state == PartnerGroupState.JOINED,
            self._muted,
        )

    async def _handle_leave_request(self) -> None:
        """处理退出请求"""
        self._state = PartnerGroupState.LEAVING

        # 发送断开状态消息，回显之前收到的 sessionId
        leave_session_id = getattr(self, "_pending_leave_session_id", None)
        try:
            await self.send_status_result(
                connected=False, muted=self._muted, session_id=leave_session_id
            )
        except Exception as exc:
            logger.warning(
                "event=partner_leave_status_failed partner_aic=%s group_id=%s error=%s",
                self.partner_aic,
                self._group_id,
                str(exc),
            )
        finally:
            self._pending_leave_session_id = None

        # 清理资源
        await self._cleanup_group_resources()

    async def send_status_result(
        self,
        connected: bool,
        muted: bool,
        session_id: Optional[str] = None,
    ) -> None:
        """发送状态结果消息

        Args:
            connected: 连接状态
            muted: 静音状态
            session_id: 会话ID（应回显收到的命令中的 sessionId，而非自行推导）
        """
        if not self._exchange:
            return
        if self._group_id is None:
            raise RuntimeError("Group not joined yet")

        result = GroupMgmtResult(
            id=f"mgmt-result-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="partner",
            senderId=self.partner_aic,
            dataItems=[],
            status=GroupMemberStatus(connected=connected, muted=muted),
            groupId=self._group_id,
            sessionId=session_id,
        )

        await self._publish_message(result)

    async def _publish_message(
        self, message: Union[TaskCommand, TaskResult, GroupMgmtCommand, GroupMgmtResult]
    ) -> None:
        """
        发布消息到群组

        注意：此方法目前未检查 _muted 状态。即使 Partner 被静音，
        仍然可以调用此方法发送消息。请业务层自觉遵守静音规则。
        """
        if not self._exchange:
            raise RuntimeError("Not connected to group")

        body = message.model_dump(exclude_none=True)
        json_bytes = json.dumps(body, ensure_ascii=False).encode()
        headers: Dict[str, str] = {}
        trace_id: str | None = None
        span_id: str | None = None
        parent_span: str = ""

        emit_ok = bool(
            self._message_emitter and body.get("type") in DATA_PLANE_TYPES
        )
        if emit_ok:
            parent = (
                _active_cmd_trace.get()
                or (self._trace_context_provider() if self._trace_context_provider else None)
            )
            trace_id = parent.trace_id if parent else new_trace_id()
            span_id = new_span_id()
            parent_span = parent.span_id if parent else ""
            headers[TRACEPARENT_HEADER] = format_traceparent(
                TraceContext(trace_id=trace_id, span_id=span_id)
            )

        amqp_message = AMQPMessage(
            body=json_bytes,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers or None,
            user_id=build_outgoing_amqp_user_id(
                message,
                local_aic=self.partner_aic,
                identity_binding_enabled=self._identity_binding_enabled,
            ),
        )

        await self._exchange.publish(amqp_message, routing_key="")
        if emit_ok and trace_id and span_id:
            await self._safe_emit(
                message_tap.build_send_body(
                    system=self._message_system,
                    exchange=self._exchange_name or "",
                    virtual_host=self.rabbitmq_vhost,
                    routing_key="",
                    message_id=body.get("id", ""),
                    payload_size_bytes=len(json_bytes),
                    attributes={
                        "groupId": self._group_id,
                        "msgType": body.get("type"),
                        "taskId": body.get("taskId"),
                    },
                ),
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span,
                correlation_id=body.get("sessionId"),
            )
        logger.debug(
            "event=message_published partner_aic=%s type=%s id=%s",
            self.partner_aic,
            body.get("type"),
            body.get("id"),
        )

    async def send_task_result(
        self,
        task_id: str,
        session_id: str,
        state: TaskState,
        products: Optional[List[Product]] = None,
        status_data_items: Optional[List[DataItem]] = None,
    ) -> None:
        """
        发送任务状态更新

        Args:
            task_id: 任务 ID
            session_id: 会话 ID
            state: 新状态
            products: 产出物列表（AwaitingCompletion 时使用）
            status_data_items: 状态附带的数据项（如错误信息、提示等）
        """
        self._task_states[task_id] = state

        result = TaskResult(
            id=f"result-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="partner",
            senderId=self.partner_aic,
            taskId=task_id,
            status=TaskStatus(
                state=state,
                stateChangedAt=datetime.now(timezone.utc).isoformat(),
                dataItems=status_data_items,
            ),
            products=products,
            groupId=self._group_id,
            sessionId=session_id,
        )

        await self._publish_message(result)
        logger.info(
            "event=task_result_sent partner_aic=%s task_id=%s state=%s",
            self.partner_aic,
            task_id,
            state,
        )

    async def accept_task(self, task_id: str, session_id: str) -> None:
        """接受任务"""
        await self.send_task_result(task_id, session_id, TaskState.Accepted)

    async def start_working(self, task_id: str, session_id: str) -> None:
        """开始工作"""
        await self.send_task_result(task_id, session_id, TaskState.Working)

    async def request_input(self, task_id: str, session_id: str, prompt: str) -> None:
        """请求更多输入"""
        await self.send_task_result(
            task_id,
            session_id,
            TaskState.AwaitingInput,
            status_data_items=[TextDataItem(text=prompt)],
        )

    async def submit_for_completion(
        self, task_id: str, session_id: str, products: List[Product]
    ) -> None:
        """提交产出物等待完成确认"""
        await self.send_task_result(
            task_id, session_id, TaskState.AwaitingCompletion, products=products
        )

    async def complete_task(self, task_id: str, session_id: str) -> None:
        """完成任务"""
        await self.send_task_result(task_id, session_id, TaskState.Completed)

    async def reject_task(self, task_id: str, session_id: str, reason: str) -> None:
        """拒绝任务"""
        await self.send_task_result(
            task_id,
            session_id,
            TaskState.Rejected,
            status_data_items=[TextDataItem(text=reason)],
        )

    async def fail_task(self, task_id: str, session_id: str, error: str) -> None:
        """任务失败"""
        await self.send_task_result(
            task_id,
            session_id,
            TaskState.Failed,
            status_data_items=[TextDataItem(text=error)],
        )

    async def cancel_task(self, task_id: str, session_id: str) -> None:
        """取消任务"""
        await self.send_task_result(task_id, session_id, TaskState.Canceled)

    def _reset_group_state(self) -> None:
        self._state = PartnerGroupState.DISCONNECTED
        self._connection = None
        self._channel = None
        self._exchange = None
        self._queue = None
        self._queue_name = None
        self._consumer_tag = None
        self._group_id = None
        self._group_info = None
        self._exchange_name = None
        self._connection_name = None
        self._vhost = None
        self._node_name = None
        self._pending_leave = False
        self._pending_leave_session_id = None
        self._muted = False
        self._task_states.clear()

    async def _cleanup_group_resources(self) -> None:
        """清理群组本地资源。"""
        if (
            self._state == PartnerGroupState.DISCONNECTED
            and self._channel is None
            and self._queue is None
            and self._connection is None
        ):
            return

        left_group_id = self._group_id
        disconnect_handler = self._disconnect_handler
        self._state = PartnerGroupState.LEAVING

        # 停止消费
        if self._queue and self._consumer_tag:
            try:
                await self._queue.cancel(self._consumer_tag)
            except Exception as e:
                logger.warning(
                    "event=consumer_cancel_failed partner_aic=%s group_id=%s error=%s",
                    self.partner_aic,
                    self._group_id,
                    str(e),
                )
            finally:
                self._consumer_tag = None
        logger.info(
            "event=consumer_stopped partner_aic=%s group_id=%s",
            self.partner_aic,
            self._group_id,
        )

        # 删除队列（允许强制删除，避免 consumer 占用导致 PRECONDITION_FAILED）
        try:
            if self._queue:
                logger.info(
                    "event=queue_delete_attempt partner_aic=%s queue=%s",
                    self.partner_aic,
                    self._queue.name,
                )
                await self._queue.delete(if_unused=False, if_empty=False)
        except Exception as e:
            logger.warning(
                "event=queue_delete_failed partner_aic=%s error=%s",
                self.partner_aic,
                str(e),
            )

        # 关闭连接
        if self._channel and not getattr(self._channel, "is_closed", False):
            await self._channel.close()
        if (
            self._connection_owner
            and self._connection
            and not getattr(self._connection, "is_closed", False)
        ):
            await self._connection.close()

        logger.info(
            "event=connection_closed partner_aic=%s group_id=%s",
            self.partner_aic,
            self._group_id,
        )

        self._reset_group_state()
        logger.info(
            "event=left_group partner_aic=%s group_id=%s",
            self.partner_aic,
            left_group_id,
        )
        if disconnect_handler and left_group_id:
            disconnect_result = disconnect_handler(self, left_group_id)
            if inspect.isawaitable(disconnect_result):
                await disconnect_result

    async def leave_group(self, session_id: Optional[str] = None) -> None:
        """主动退出群组。

        主动退出时先广播 LEAVE_GROUP 命令，再发送 connected=false 状态，
        最后执行本地资源清理。
        """
        if self._state == PartnerGroupState.DISCONNECTED:
            return

        self._state = PartnerGroupState.LEAVING

        if self._group_id and self._exchange:
            leave_command = GroupMgmtCommand(
                id=f"mgmt-cmd-{uuid.uuid4()}",
                sentAt=datetime.now(timezone.utc).isoformat(),
                senderRole="partner",
                senderId=self.partner_aic,
                dataItems=[],
                command=GroupMgmtCommandType.LEAVE_GROUP,
                mentions=[self.partner_aic],
                groupId=self._group_id,
                sessionId=session_id,
            )
            try:
                await self._publish_message(leave_command)
            except Exception as exc:
                logger.warning(
                    "event=partner_leave_command_failed partner_aic=%s group_id=%s error=%s",
                    self.partner_aic,
                    self._group_id,
                    str(exc),
                )

            try:
                await self.send_status_result(
                    connected=False,
                    muted=self._muted,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.warning(
                    "event=partner_leave_status_failed partner_aic=%s group_id=%s error=%s",
                    self.partner_aic,
                    self._group_id,
                    str(exc),
                )

        await self._cleanup_group_resources()

    async def close(self) -> None:
        """关闭客户端"""
        if self._state != PartnerGroupState.DISCONNECTED:
            await self.leave_group()
        if self._inbox_channel and not getattr(self._inbox_channel, "is_closed", False):
            await self._inbox_channel.close()
        if (
            self._connection_owner
            and self._connection
            and not getattr(self._connection, "is_closed", False)
        ):
            await self._connection.close()


class PartnerGroupSession:
    """
    Partner 群组会话

    封装单个 Partner 在群组中的会话状态和任务管理
    """

    def __init__(self, partner_aic: str, client: GroupPartnerMqClient):
        self.partner_aic = partner_aic
        self.client = client
        self.created_at = datetime.now(timezone.utc).isoformat()

        # 任务状态跟踪
        # task_id -> { state, products, ... }
        self.tasks: Dict[str, Dict[str, Any]] = {}

        # 消息历史
        self.message_history: List[Union[TaskCommand, TaskResult, GroupMgmtCommand]] = (
            []
        )

    def record_message(
        self, message: Union[TaskCommand, TaskResult, GroupMgmtCommand]
    ) -> None:
        """记录消息"""
        self.message_history.append(message)

    def update_task(
        self,
        task_id: str,
        state: TaskState,
        products: Optional[List[Product]] = None,
    ) -> None:
        """更新任务状态"""
        if task_id not in self.tasks:
            self.tasks[task_id] = {}
        self.tasks[task_id]["state"] = state
        self.tasks[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        if products:
            self.tasks[task_id]["products"] = products

    def get_task_state(self, task_id: str) -> Optional[TaskState]:
        """获取任务状态"""
        return self.tasks.get(task_id, {}).get("state")


def extract_text_from_command(command: TaskCommand) -> str:
    """
    辅助函数：从任务命令中提取文本内容

    将命令中所有 TextDataItem 的文本拼接起来
    """
    texts = []
    if command.dataItems:
        for item in command.dataItems:
            text = getattr(item, "text", None)
            if text:
                texts.append(str(text))
    return "\n".join(texts)
