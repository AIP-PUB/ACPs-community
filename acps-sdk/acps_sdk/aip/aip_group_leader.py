"""
群组模式客户端模块 - Leader角色的消息队列通信实现

本模块实现了AIP协议中群组模式(Group Mode)下Leader角色的核心功能：
1. 连接RabbitMQ消息队列服务器
2. 创建群组Exchange和队列
3. 发送群组消息（任务消息、管理消息）
4. 接收和处理群组内的消息
5. 管理群组成员（邀请加入、踢出、静音等）
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Union, TYPE_CHECKING

import aio_pika
import httpx
from aio_pika import ExchangeType
from aio_pika import Message as AMQPMessage
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)
from aio_pika.exceptions import DeliveryError

from .aip_base_model import (
    DataItem,
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TextDataItem,
)
from .aip_group_auth import GroupAclClient, GroupAclError
from .aip_group_identity import (
    assert_incoming_group_message_identity,
    build_outgoing_amqp_user_id,
    extract_amqp_user_id,
)
from .aip_group_model import (
    ACSObject,
    AMQPConfig,
    GroupInfo,
    GroupMgmtCommand,
    GroupMgmtCommandType,
    GroupMgmtResult,
    InboxGroupInvitation,
    InboxGroupInvitationError,
    RabbitMQRequest,
    RabbitMQRequestParams,
    RabbitMQResponse,
    RabbitMQServerConfig,
)
from .aip_group_runtime import (
    DEFAULT_INVITATION_TIMEOUT_SECONDS,
    INBOX_EXCHANGE_NAME,
    INBOX_MESSAGE_TTL_MS,
    INBOX_QUEUE_EXPIRES_MS,
    build_external_connection_url,
    build_group_exchange_name,
    build_group_queue_name,
    build_inbox_queue_name,
    build_plain_connection_url,
    calculate_invitation_expiry,
    create_invitation_token,
    ensure_valid_group_id,
    get_endpoint_url,
    normalize_group_id,
    parse_amqp_endpoint_url,
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

logger = logging.getLogger(__name__)

DATA_PLANE_TYPES = frozenset({"task-command", "task-result"})


class GroupLeaderState(str, Enum):
    """群组Leader状态枚举"""

    DISCONNECTED = "DISCONNECTED"
    READY = "READY"
    DISSOLVING = "DISSOLVING"


class PartnerInviteError(Exception):
    """Partner邀请失败基类"""

    pass


class PartnerNetworkError(PartnerInviteError):
    """Partner邀请网络错误"""

    pass


class PartnerResponseError(PartnerInviteError):
    """Partner返回错误响应"""

    pass


@dataclass
class PendingInvitation:
    session_id: str
    partner_aic: str
    invitation_token: str
    future: asyncio.Future["PartnerConnectionInfo"]
    timeout_task: asyncio.Task[None]


class PartnerConnectionInfo:
    """Partner连接信息"""

    def __init__(
        self,
        aic: str,
        connection_name: str,
        vhost: str,
        node_name: str,
        queue_name: str,
        process_id: Optional[str] = None,
    ):
        self.aic = aic
        self.connection_name = connection_name
        self.vhost = vhost
        self.node_name = node_name
        self.queue_name = queue_name
        self.process_id = process_id
        self.connected = True
        self.muted = False
        self.joined_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aic": self.aic,
            "connection_name": self.connection_name,
            "vhost": self.vhost,
            "node_name": self.node_name,
            "queue_name": self.queue_name,
            "process_id": self.process_id,
            "connected": self.connected,
            "muted": self.muted,
            "joined_at": self.joined_at,
        }


# 消息处理回调类型
MessageHandler = Callable[
    [Union[TaskCommand, TaskResult, GroupMgmtCommand, GroupMgmtResult]],
    Awaitable[None],
]


class GroupLeaderMqClient:
    """
    群组模式Leader客户端

    负责：
    - 连接到RabbitMQ服务器
    - 创建和管理群组Exchange
    - 发送任务消息和管理消息到群组
    - 接收群组内的消息
    - 管理Partner成员
    """

    def __init__(
        self,
        leader_aic: str,
        rabbitmq_host: str = "localhost",
        rabbitmq_port: int = 5672,
        rabbitmq_vhost: str = "/",
        rabbitmq_user: Optional[str] = "guest",
        rabbitmq_password: Optional[str] = "guest",
        ssl_context: Optional[ssl.SSLContext] = None,
        connection: Optional[AbstractConnection] = None,
        connection_owner: bool = True,
        *,
        message_emitter: MessageEmitter | None = None,
        message_system: str = "rabbitmq",
        trace_context_provider: Callable[[], TraceContext | None] | None = None,
        identity_binding_enabled: bool = True,
    ):
        """
        初始化Leader客户端

        Args:
            leader_aic: Leader的AIC标识
            rabbitmq_host: RabbitMQ服务器地址
            rabbitmq_port: RabbitMQ服务器端口
            rabbitmq_vhost: 虚拟主机
            rabbitmq_user: 用户名
            rabbitmq_password: 密码
            ssl_context: SSL上下文（可选）
        """
        self.leader_aic = leader_aic
        self.rabbitmq_host = rabbitmq_host
        self.rabbitmq_port = rabbitmq_port
        self.rabbitmq_vhost = rabbitmq_vhost
        self.rabbitmq_user = rabbitmq_user or None
        self.rabbitmq_password = rabbitmq_password or None
        self.ssl_context = ssl_context
        self._connection_owner = connection_owner
        self._message_emitter = message_emitter
        self._message_system = message_system
        self._trace_context_provider = trace_context_provider
        self._identity_binding_enabled = identity_binding_enabled
        if not self._identity_binding_enabled:
            logger.warning(
                "AIP identity binding disabled for GroupLeaderMqClient leader_aic=%s",
                self.leader_aic,
            )

        # 连接和通道
        self._connection: Optional[AbstractConnection] = connection
        self._channel: Optional[AbstractChannel] = None

        # 群组信息
        self._group_id: Optional[str] = None
        self._exchange_name: Optional[str] = None
        self._exchange: Optional[AbstractExchange] = None
        self._leader_queue: Optional[AbstractQueue] = None
        self._state: GroupLeaderState = GroupLeaderState.DISCONNECTED

        # Partner管理
        self._partners: Dict[str, PartnerConnectionInfo] = (
            {}
        )  # aic -> PartnerConnectionInfo
        self._partner_acs: List[ACSObject] = []  # Partner的ACS信息列表

        # 消息处理器
        self._message_handler: Optional[MessageHandler] = None
        self._consume_task: Optional[asyncio.Task] = None

    @property
    def group_id(self) -> Optional[str]:
        return self._group_id

    @property
    def state(self) -> GroupLeaderState:
        return self._state

    @property
    def partners(self) -> Dict[str, PartnerConnectionInfo]:
        return self._partners.copy()

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def connect(self) -> None:
        """建立到RabbitMQ的连接"""
        if self._connection and not self._connection.is_closed:
            if self._channel is None or getattr(self._channel, "is_closed", False):
                self._channel = await self._connection.channel()
            return

        if self._connection and getattr(self._connection, "is_closed", False):
            if not self._connection_owner:
                raise RuntimeError(
                    "Shared connection is closed; owner must recreate it"
                )

        connection_name = f"agent-{self.leader_aic}"
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

        try:
            self._connection = await aio_pika.connect_robust(
                connection_url,
                **connection_kwargs,
            )
            self._channel = await self._connection.channel()
            logger.info(
                "event=rabbitmq_connected leader_aic=%s host=%s vhost=%s",
                self.leader_aic,
                self.rabbitmq_host,
                self.rabbitmq_vhost,
            )
        except Exception as e:
            logger.error(
                "event=rabbitmq_connection_failed leader_aic=%s error=%s",
                self.leader_aic,
                str(e),
            )
            raise

    async def create_group(
        self,
        group_id: Optional[str] = None,
        partner_acs_list: Optional[List[ACSObject]] = None,
    ) -> str:
        """
        创建群组

        Args:
            group_id: 群组ID，如果不提供则自动生成
            partner_acs_list: Partner的ACS信息列表

        Returns:
            群组ID
        """
        if not self.is_connected:
            await self.connect()

        resolved_group_id = ensure_valid_group_id(group_id or f"group-{uuid.uuid4()}")
        self._group_id = resolved_group_id
        self._exchange_name = build_group_exchange_name(
            self.leader_aic, resolved_group_id
        )
        self._partner_acs = partner_acs_list or []
        channel = self._channel
        if channel is None:
            raise RuntimeError("RabbitMQ channel is not ready")

        # 创建fanout类型的Exchange用于群组广播
        # auto_delete=True：当最后一个绑定队列解绑后自动删除，避免会话结束后 Exchange 残留
        self._exchange = await channel.declare_exchange(
            name=self._exchange_name,
            type=ExchangeType.FANOUT,
            durable=True,
            auto_delete=True,
        )
        logger.info(
            "event=exchange_created group_id=%s exchange=%s",
            self._group_id,
            self._exchange_name,
        )

        # 创建Leader的队列
        leader_queue_name = build_group_queue_name(
            self.leader_aic, self._group_id, self.leader_aic
        )
        self._leader_queue = await channel.declare_queue(
            name=leader_queue_name,
            durable=True,
            exclusive=False,
            auto_delete=True,
        )

        # 绑定队列到Exchange
        await self._leader_queue.bind(self._exchange)
        logger.info(
            "event=leader_queue_created group_id=%s queue=%s",
            self._group_id,
            leader_queue_name,
        )

        self._state = GroupLeaderState.READY
        return self._group_id

    def set_message_handler(self, handler: MessageHandler) -> None:
        """设置消息处理回调"""
        self._message_handler = handler

    async def _safe_emit(self, body: Any, **trace_kwargs: str | None) -> None:
        if not self._message_emitter:
            return
        try:
            await self._message_emitter.emit(body, **trace_kwargs)
        except Exception:
            logger.warning("message emit failed", exc_info=True)

    async def start_consuming(self) -> None:
        """开始消费消息"""
        if not self._leader_queue:
            raise RuntimeError("Group not created yet")

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
                    msg_type = body.get("type", "unknown")
                    sender_id = body.get("senderId", "unknown")
                    logger.debug(
                        "[GroupLeaderMq] >>> Message received: group_id=%s type=%s sender=%s",
                        self._group_id,
                        msg_type,
                        sender_id,
                    )
                    emit_ok = bool(
                        self._message_emitter
                        and body.get("type") in DATA_PLANE_TYPES
                        and body.get("senderId") != self.leader_aic
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
                                queue_name=self._leader_queue.name,
                                message_id=body.get("id", ""),
                                payload_size_bytes=len(message.body),
                                delivery_attempt=2 if message.redelivered else 1,
                                attributes={
                                    "groupId": self._group_id,
                                    "msgType": body.get("type"),
                                    "senderId": body.get("senderId"),
                                },
                            ),
                            trace_id=trace_id,
                            span_id=recv_span,
                            parent_span_id=ctx.span_id if ctx else "",
                            correlation_id=body.get("sessionId"),
                        )
                    parsed_message = self._parse_incoming_message(body)
                    if parsed_message:
                        if self._message_handler:
                            logger.debug(
                                "[GroupLeaderMq] Calling message handler for type=%s",
                                msg_type,
                            )
                            await self._message_handler(parsed_message)
                        else:
                            logger.warning(
                                "[GroupLeaderMq] No message handler set, discarding message type=%s",
                                msg_type,
                            )
                    else:
                        logger.warning(
                            "[GroupLeaderMq] Failed to parse message type=%s",
                            msg_type,
                        )
                except Exception as e:
                    if emit_ok and trace_id and recv_span:
                        await self._safe_emit(
                            message_tap.build_settlement_body(
                                "nack",
                                system=self._message_system,
                                exchange=self._exchange_name or "",
                                virtual_host=self.rabbitmq_vhost,
                                queue_name=self._leader_queue.name,
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
                    logger.error(
                        "event=message_processing_error group_id=%s error=%s",
                        self._group_id,
                        str(e),
                    )
                    import traceback

                    logger.error(
                        "[GroupLeaderMq] Traceback: %s", traceback.format_exc()
                    )
                    raise
                if emit_ok and trace_id and recv_span:
                    await self._safe_emit(
                        message_tap.build_settlement_body(
                            "ack",
                            system=self._message_system,
                            exchange=self._exchange_name or "",
                            virtual_host=self.rabbitmq_vhost,
                            queue_name=self._leader_queue.name,
                            message_id=body.get("id", ""),
                            delivery_attempt=2 if message.redelivered else 1,
                            latency_ms=(monotonic() - t0) * 1000,
                        ),
                        trace_id=trace_id,
                        span_id=new_span_id(),
                        parent_span_id=recv_span,
                        correlation_id=body.get("sessionId"),
                    )

        self._consume_task = asyncio.create_task(self._leader_queue.consume(on_message))
        logger.info("event=consuming_started group_id=%s", self._group_id)

    def _parse_incoming_message(
        self, body: Dict[str, Any]
    ) -> Optional[Union[TaskCommand, TaskResult, GroupMgmtCommand, GroupMgmtResult]]:
        """解析接收到的消息"""
        msg_type = body.get("type")

        try:
            if msg_type == "group-mgmt-command":
                return GroupMgmtCommand.model_validate(body)
            elif msg_type == "group-mgmt-result":
                return GroupMgmtResult.model_validate(body)
            elif msg_type == "task-result":
                return TaskResult.model_validate(body)
            elif msg_type == "task-command":
                return TaskCommand.model_validate(body)
            else:
                logger.warning(
                    "event=unknown_message_type group_id=%s type=%s",
                    self._group_id,
                    msg_type,
                )
                return None
        except Exception as e:
            logger.error(
                "event=message_parse_error group_id=%s error=%s body=%s",
                self._group_id,
                str(e),
                str(body)[:200],
            )
            return None

    async def invite_partner(
        self,
        partner_acs: ACSObject,
        partner_rpc_url: str,
        ssl_context: Optional[ssl.SSLContext] = None,
        client_cert=None,
        timeout: float = 30.0,
    ) -> PartnerConnectionInfo:
        """
        邀请Partner加入群组

        通过RPC方式向Partner发送群组邀请请求

        Args:
            partner_acs: Partner的ACS信息
            partner_rpc_url: Partner的RPC端点URL
            ssl_context: SSL上下文
            timeout: 超时时间（秒）

        Returns:
            Partner连接信息

        Raises:
            PartnerNetworkError: 网络请求失败
            PartnerResponseError: Partner拒绝或返回错误
            PartnerInviteError: 其他邀请错误
        """
        if self._state != GroupLeaderState.READY:
            raise RuntimeError(f"Cannot invite partner in state: {self._state}")

        # 构造Leader的ACS对象
        leader_acs = ACSObject(aic=self.leader_aic)
        group_id = self._group_id
        exchange_name = self._exchange_name
        if group_id is None or exchange_name is None:
            raise RuntimeError("Group not created yet")

        # 构造群组信息
        group_info = GroupInfo(
            groupId=group_id,
            leader=leader_acs,
            partners=self._partner_acs + [partner_acs],
        )

        access_token: Optional[str] = None
        if self.rabbitmq_user and self.rabbitmq_password:
            access_token = f"{self.rabbitmq_user}:{self.rabbitmq_password}"

        server_config = RabbitMQServerConfig(
            host=self.rabbitmq_host,
            port=self.rabbitmq_port,
            vhost=self.rabbitmq_vhost,
            accessToken=access_token,
        )

        # 构造AMQP配置
        amqp_config = AMQPConfig(
            exchange=exchange_name, exchangeType="fanout", routingKey=""
        )

        # 构造RPC请求
        request = RabbitMQRequest(
            id=str(uuid.uuid4()),
            params=RabbitMQRequestParams(
                protocol="rabbitmq:4.0",
                group=group_info,
                server=server_config,
                amqp=amqp_config,
            ),
        )

        # 发送RPC请求（带重试）
        max_attempts = 3
        backoff_seconds = 1.0

        try:
            async with httpx.AsyncClient(
                verify=ssl_context if ssl_context is not None else True,
                cert=client_cert,
                timeout=timeout,
            ) as client:
                for attempt in range(1, max_attempts + 1):
                    try:
                        logger.info(
                            "event=partner_invite_request partner_aic=%s url=%s attempt=%d",
                            partner_acs.aic,
                            partner_rpc_url,
                            attempt,
                        )
                        response = await client.post(
                            partner_rpc_url,
                            json=request.model_dump(exclude_none=True),
                            headers={"Content-Type": "application/json"},
                        )
                        response.raise_for_status()
                        break
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        if status >= 500 and attempt < max_attempts:
                            logger.warning(
                                "event=partner_invite_retry partner_aic=%s url=%s status=%s attempt=%d",
                                partner_acs.aic,
                                partner_rpc_url,
                                status,
                                attempt,
                            )
                            await asyncio.sleep(backoff_seconds)
                            backoff_seconds *= 2
                            continue
                        raise
                    except httpx.RequestError:
                        if attempt < max_attempts:
                            logger.warning(
                                "event=partner_invite_retry partner_aic=%s url=%s attempt=%d",
                                partner_acs.aic,
                                partner_rpc_url,
                                attempt,
                            )
                            await asyncio.sleep(backoff_seconds)
                            backoff_seconds *= 2
                            continue
                        raise

                rpc_response = RabbitMQResponse.model_validate(response.json())

                if rpc_response.error:
                    error_msg = rpc_response.error.message
                    logger.error(
                        "event=partner_invite_failed partner_aic=%s error=%s",
                        partner_acs.aic,
                        error_msg,
                    )
                    raise PartnerResponseError(f"Partner refused: {error_msg}")

                if rpc_response.result:
                    result = rpc_response.result
                    partner_info = PartnerConnectionInfo(
                        aic=partner_acs.aic,
                        connection_name=result.connectionName,
                        vhost=result.vhost,
                        node_name=result.nodeName,
                        queue_name=result.queueName,
                        process_id=result.processId,
                    )
                    self._partners[partner_acs.aic] = partner_info
                    self._partner_acs.append(partner_acs)

                    logger.info(
                        "event=partner_joined group_id=%s partner_aic=%s",
                        self._group_id,
                        partner_acs.aic,
                    )
                    return partner_info

                raise PartnerResponseError("Empty result in response")

        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error(
                "event=partner_invite_network_error partner_aic=%s url=%s error=%s",
                partner_acs.aic,
                partner_rpc_url,
                str(e),
            )
            raise PartnerNetworkError(f"Network error: {str(e)}") from e
        except PartnerInviteError:
            raise
        except Exception as e:
            logger.error(
                "event=partner_invite_exception partner_aic=%s url=%s error_type=%s error=%s",
                partner_acs.aic,
                partner_rpc_url,
                type(e).__name__,
                repr(e),
            )
            raise PartnerInviteError(f"Unexpected error: {str(e)}") from e

    async def invite_via_inbox(
        self,
        partner_acs: ACSObject,
        *,
        partner_inbox: str,
        invitation_token: Optional[str] = None,
        expires_at: Optional[str] = None,
        timeout_seconds: int = DEFAULT_INVITATION_TIMEOUT_SECONDS,
    ) -> InboxGroupInvitation:
        if self._state != GroupLeaderState.READY:
            raise RuntimeError(f"Cannot invite partner in state: {self._state}")
        if not self._channel or not self._exchange_name or not self._group_id:
            raise RuntimeError("Group not created yet")

        invitation = InboxGroupInvitation(
            id=f"invite-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=self.leader_aic,
            groupId=self._group_id,
            protocol="rabbitmq:4.2",
            expiresAt=expires_at or calculate_invitation_expiry(timeout_seconds),
            invitationToken=invitation_token or create_invitation_token(),
            group=GroupInfo(
                groupId=self._group_id,
                leader=ACSObject(aic=self.leader_aic),
                partners=self._partner_acs + [partner_acs],
            ),
            amqp=AMQPConfig(
                exchange=self._exchange_name,
                exchangeType=ExchangeType.FANOUT.value,
                routingKey="",
            ),
        )

        inbox_exchange = await self._channel.declare_exchange(
            name=INBOX_EXCHANGE_NAME,
            type=ExchangeType.TOPIC,
            durable=True,
            auto_delete=False,
        )
        message = AMQPMessage(
            body=json.dumps(
                invitation.model_dump(exclude_none=True), ensure_ascii=False
            ).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            user_id=build_outgoing_amqp_user_id(
                invitation,
                local_aic=self.leader_aic,
                identity_binding_enabled=self._identity_binding_enabled,
            ),
        )
        await inbox_exchange.publish(message, routing_key=partner_inbox)
        logger.info(
            "event=partner_invite_inbox_sent group_id=%s partner_aic=%s inbox=%s",
            self._group_id,
            partner_acs.aic,
            partner_inbox,
        )
        return invitation

    async def publish_message(
        self, message: Union[TaskCommand, TaskResult, GroupMgmtCommand]
    ) -> None:
        """
        发布消息到群组

        Args:
            message: 要发布的消息
        """
        if not self._exchange:
            raise RuntimeError("Group not created yet")

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
                self._trace_context_provider() if self._trace_context_provider else None
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
                local_aic=self.leader_aic,
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
        logger.info(
            "event=message_published group_id=%s type=%s id=%s mentions=%s",
            self._group_id,
            body.get("type"),
            body.get("id"),
            body.get("mentions"),
        )

    async def send_task_command(
        self,
        command: TaskCommandType,
        task_id: str,
        session_id: str,
        text_content: Optional[str] = None,
        data_items: Optional[List[DataItem]] = None,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
    ) -> TaskCommand:
        """
        发送任务命令

        Args:
            command: 任务命令类型
            task_id: 任务ID
            session_id: 会话ID
            text_content: 文本内容
            data_items: 数据项列表
            mentions: 提及的Partner列表

        Returns:
            发送的命令对象
        """
        items = data_items or []
        if text_content:
            items.insert(0, TextDataItem(text=text_content))

        task_command = TaskCommand(
            id=f"cmd-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=self.leader_aic,
            command=command,
            dataItems=items,
            taskId=task_id,
            groupId=self._group_id,
            sessionId=session_id,
            mentions=mentions,
        )

        await self.publish_message(task_command)
        return task_command

    async def start_task(
        self,
        session_id: str,
        text_content: str,
        task_id: Optional[str] = None,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
    ) -> str:
        """
        在群组中启动新任务

        Args:
            session_id: 会话ID
            text_content: 任务内容
            task_id: 任务ID（可选，不提供则自动生成）
            mentions: 指定处理任务的Partner

        Returns:
            任务ID
        """
        task_id = task_id or f"task-{uuid.uuid4()}"

        await self.send_task_command(
            command=TaskCommandType.Start,
            task_id=task_id,
            session_id=session_id,
            text_content=text_content,
            mentions=mentions,
        )

        logger.info(
            "event=task_started group_id=%s task_id=%s mentions=%s",
            self._group_id,
            task_id,
            mentions,
        )
        return task_id

    async def continue_task(
        self,
        task_id: str,
        session_id: str,
        text_content: str,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
    ) -> None:
        """继续任务"""
        await self.send_task_command(
            command=TaskCommandType.Continue,
            task_id=task_id,
            session_id=session_id,
            text_content=text_content,
            mentions=mentions,
        )
        logger.info(
            "event=task_continue_sent group_id=%s task_id=%s mentions=%s",
            self._group_id,
            task_id,
            mentions,
        )

    async def complete_task(
        self,
        task_id: str,
        session_id: str,
        text_content: Optional[str] = None,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
    ) -> None:
        """完成任务"""
        await self.send_task_command(
            command=TaskCommandType.Complete,
            task_id=task_id,
            session_id=session_id,
            text_content=text_content,
            mentions=mentions,
        )
        logger.info(
            "event=task_complete_sent group_id=%s task_id=%s mentions=%s",
            self._group_id,
            task_id,
            mentions,
        )

    async def cancel_task(
        self,
        task_id: str,
        session_id: str,
        reason: Optional[str] = None,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
    ) -> None:
        """取消任务"""
        await self.send_task_command(
            command=TaskCommandType.Cancel,
            task_id=task_id,
            session_id=session_id,
            text_content=reason,
            mentions=mentions,
        )

    async def send_mgmt_command(
        self,
        command: GroupMgmtCommandType,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
        session_id: Optional[str] = None,
    ) -> GroupMgmtCommand:
        """
        发送群组管理命令

        Args:
            command: 管理命令类型
            mentions: 目标成员
            session_id: 会话ID（可选），用于将管理操作关联到特定会话

        Returns:
            发送的管理命令对象
        """
        mgmt_command = self._build_mgmt_command(
            command=command,
            mentions=mentions,
            session_id=session_id,
        )

        await self.publish_message(mgmt_command)
        return mgmt_command

    def _build_mgmt_command(
        self,
        command: GroupMgmtCommandType,
        mentions: Optional[Union[Literal["all"], List[str]]] = None,
        session_id: Optional[str] = None,
    ) -> GroupMgmtCommand:
        if not self._group_id:
            raise RuntimeError("Group not created yet")

        return GroupMgmtCommand(
            id=f"mgmt-cmd-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=self.leader_aic,
            dataItems=[],
            command=command,
            mentions=mentions,
            groupId=self._group_id,
            sessionId=session_id,
        )

    async def get_partner_status(
        self,
        partner_aic: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> GroupMgmtCommand:
        """
        获取Partner状态

        Args:
            partner_aic: 指定Partner的AIC，如果不提供则查询所有Partner
            session_id: 会话ID，用于将管理操作关联到特定会话
        """
        mentions: Union[Literal["all"], List[str]]
        mentions = [partner_aic] if partner_aic else "all"
        return await self.send_mgmt_command(
            command=GroupMgmtCommandType.GET_STATUS,
            mentions=mentions,
            session_id=session_id,
        )

    async def request_partner_leave(
        self, partner_aic: str, session_id: Optional[str] = None
    ) -> GroupMgmtCommand:
        """
        要求Partner退出群组

        Args:
            partner_aic: Partner的AIC
            session_id: 会话ID，用于将管理操作关联到特定会话
        """
        max_attempts = 3
        retry_delay_seconds = 0.2
        last_error: DeliveryError | None = None

        def _build_leave_noop(reason: str) -> GroupMgmtCommand:
            logger.info(
                "event=partner_leave_noop group_id=%s partner_aic=%s session_id=%s reason=%s",
                self._group_id,
                partner_aic,
                session_id,
                reason,
            )
            return self._build_mgmt_command(
                command=GroupMgmtCommandType.LEAVE_GROUP,
                mentions=[partner_aic],
                session_id=session_id,
            )

        for attempt in range(1, max_attempts + 1):
            partner = self._partners.get(partner_aic)
            if partner is not None and not partner.connected:
                return _build_leave_noop("already_disconnected")

            try:
                return await self.send_mgmt_command(
                    command=GroupMgmtCommandType.LEAVE_GROUP,
                    mentions=[partner_aic],
                    session_id=session_id,
                )
            except DeliveryError as exc:
                last_error = exc
                logger.warning(
                    "event=partner_leave_publish_retry group_id=%s partner_aic=%s session_id=%s attempt=%d max_attempts=%d error=%s",
                    self._group_id,
                    partner_aic,
                    session_id,
                    attempt,
                    max_attempts,
                    str(exc),
                )
                partner = self._partners.get(partner_aic)
                if partner is not None and not partner.connected:
                    return _build_leave_noop("disconnected_after_publish_error")
                if attempt == max_attempts:
                    break
                await asyncio.sleep(retry_delay_seconds * attempt)

        if last_error is not None:
            partner = self._partners.get(partner_aic)
            if partner is not None and not partner.connected:
                return _build_leave_noop("disconnected_after_retries")
            raise last_error

        raise RuntimeError("partner leave publish failed without a DeliveryError")

    async def delete_partner_queue(self, partner_aic: str) -> bool:
        """删除指定 Partner 的群组队列。"""
        if not self._group_id:
            raise RuntimeError("Group not created yet")
        if self._channel is None:
            raise RuntimeError("RabbitMQ channel is not ready")

        queue_name = build_group_queue_name(
            self.leader_aic, self._group_id, partner_aic
        )
        try:
            await self._channel.queue_delete(
                queue_name,
                if_unused=False,
                if_empty=False,
            )
        except Exception as exc:
            logger.warning(
                "event=partner_queue_delete_failed group_id=%s partner_aic=%s queue=%s error=%s",
                self._group_id,
                partner_aic,
                queue_name,
                str(exc),
            )
            return False

        logger.info(
            "event=partner_queue_deleted group_id=%s partner_aic=%s queue=%s",
            self._group_id,
            partner_aic,
            queue_name,
        )
        return True

    def remove_partner_tracking(self, partner_aic: str) -> None:
        """从 Leader 本地状态中移除指定 Partner。"""
        removed = self._partners.pop(partner_aic, None)
        self._partner_acs = [acs for acs in self._partner_acs if acs.aic != partner_aic]
        logger.info(
            "event=partner_tracking_removed group_id=%s partner_aic=%s had_runtime_state=%s",
            self._group_id,
            partner_aic,
            removed is not None,
        )

    async def mute_partner(
        self, partner_aic: str, session_id: Optional[str] = None
    ) -> GroupMgmtCommand:
        """静音Partner

        Args:
            partner_aic: Partner的AIC
            session_id: 会话ID，用于将管理操作关联到特定会话
        """
        msg = await self.send_mgmt_command(
            command=GroupMgmtCommandType.MUTE,
            mentions=[partner_aic],
            session_id=session_id,
        )
        if partner_aic in self._partners:
            self._partners[partner_aic].muted = True
        return msg

    async def unmute_partner(
        self, partner_aic: str, session_id: Optional[str] = None
    ) -> GroupMgmtCommand:
        """取消静音Partner

        Args:
            partner_aic: Partner的AIC
            session_id: 会话ID，用于将管理操作关联到特定会话
        """
        msg = await self.send_mgmt_command(
            command=GroupMgmtCommandType.UNMUTE,
            mentions=[partner_aic],
            session_id=session_id,
        )
        if partner_aic in self._partners:
            self._partners[partner_aic].muted = False
        return msg

    def handle_partner_status_update(
        self, mgmt_result: Union[GroupMgmtCommand, GroupMgmtResult]
    ) -> None:
        """
        处理Partner状态更新消息

        Args:
            mgmt_result: Partner发送的状态消息（GroupMgmtResult 包含 status）
        """
        sender_id = mgmt_result.senderId
        # GroupMgmtResult 才有 status 属性
        status = getattr(mgmt_result, "status", None)
        if sender_id and status:
            partner = self._partners.get(sender_id)
            if partner is None and self._group_id:
                partner = PartnerConnectionInfo(
                    aic=sender_id,
                    connection_name=f"agent-{sender_id}",
                    vhost=self.rabbitmq_vhost,
                    node_name="unknown",
                    queue_name=build_group_queue_name(
                        self.leader_aic, self._group_id, sender_id
                    ),
                )
                self._partners[sender_id] = partner

            if partner is None:
                return

            partner.connected = status.connected
            partner.muted = status.muted

            if not status.connected:
                logger.info(
                    "event=partner_disconnected group_id=%s partner_aic=%s",
                    self._group_id,
                    sender_id,
                )

            if (
                isinstance(mgmt_result, GroupMgmtResult)
                and status.connected
                and all(acs.aic != sender_id for acs in self._partner_acs)
            ):
                self._partner_acs.append(ACSObject(aic=sender_id))

    async def broadcast_group_shutdown(self) -> None:
        if self._state == GroupLeaderState.DISCONNECTED:
            return

        try:
            await self.send_mgmt_command(
                command=GroupMgmtCommandType.DISBAND, mentions="all"
            )
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(
                "event=dissolve_notify_failed group_id=%s error=%s",
                self._group_id,
                str(e),
            )

    async def cleanup_group_resources(self) -> None:
        if self._consume_task:
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass

        if self._leader_queue:
            try:
                if self._channel is None:
                    raise RuntimeError("RabbitMQ channel is not ready")
                logger.info(
                    "event=queue_delete_attempt group_id=%s queue=%s",
                    self._group_id,
                    self._leader_queue.name,
                )
                await self._channel.queue_delete(
                    self._leader_queue.name,
                    if_unused=False,
                    if_empty=False,
                )
            except Exception as e:
                logger.warning(
                    "event=queue_cleanup_failed group_id=%s queue=%s error=%s",
                    self._group_id,
                    self._leader_queue.name,
                    str(e),
                )
            finally:
                self._leader_queue = None

        if self._exchange_name:
            try:
                if self._channel is None:
                    raise RuntimeError("RabbitMQ channel is not ready")
                logger.info(
                    "event=exchange_delete_attempt group_id=%s exchange=%s",
                    self._group_id,
                    self._exchange_name,
                )
                await self._channel.exchange_delete(
                    self._exchange_name,
                    if_unused=False,
                )
            except Exception as e:
                logger.warning(
                    "event=exchange_cleanup_failed group_id=%s exchange=%s error=%s",
                    self._group_id,
                    self._exchange_name,
                    str(e),
                )
            finally:
                self._exchange = None
                self._exchange_name = None

    async def dissolve_group(self) -> None:
        """解散群组"""
        if self._state == GroupLeaderState.DISCONNECTED:
            return

        self._state = GroupLeaderState.DISSOLVING

        await self.broadcast_group_shutdown()

        await self.cleanup_group_resources()

        if self._channel and not getattr(self._channel, "is_closed", False):
            await self._channel.close()
        if (
            self._connection_owner
            and self._connection
            and not getattr(self._connection, "is_closed", False)
        ):
            await self._connection.close()

        self._state = GroupLeaderState.DISCONNECTED
        self._partners.clear()

        logger.info("event=group_dissolved group_id=%s", self._group_id)

    async def close(self) -> None:
        """关闭客户端连接"""
        if self._state != GroupLeaderState.DISCONNECTED:
            await self.dissolve_group()


class GroupLeaderSession:
    """
    群组会话管理

    封装一个完整的群组会话，包含：
    - 群组客户端
    - 会话状态
    - 任务状态跟踪
    - 消息历史
    """

    def __init__(
        self,
        session_id: str,
        group_id: str,
        leader_mq_client: GroupLeaderMqClient,
    ):
        self.MAX_HISTORY = 1000
        self.session_id = session_id
        self.group_id = group_id
        self.leader_mq_client = leader_mq_client
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.last_modified = datetime.now(timezone.utc)

        # 任务状态跟踪
        # task_id -> { partner_aic -> TaskState }
        self.task_states: Dict[str, Dict[str, TaskState]] = {}

        # Partner产出物
        # task_id -> { partner_aic -> product_text }
        self.task_products: Dict[str, Dict[str, str]] = {}

        # Partner等待输入的提示信息
        # task_id -> { partner_aic -> AwaitingInput 提示 }
        self.task_prompts: Dict[str, Dict[str, Optional[str]]] = {}

        # 消息历史
        self.message_history: List[
            Union[TaskCommand, TaskResult, GroupMgmtCommand, GroupMgmtResult]
        ] = []

        # 状态更新事件
        self.state_update_event = asyncio.Event()

        # Partner 最近一次邀请路由
        self.invitation_routes: Dict[str, str] = {}

    def touch(self) -> None:
        """更新最后修改时间"""
        self.last_modified = datetime.now(timezone.utc)

    def record_invitation_route(self, partner_aic: str, route: str) -> None:
        """记录 Partner 的邀请路由。"""
        self.touch()
        self.invitation_routes[partner_aic] = route

    def record_task_state(
        self,
        task_id: str,
        partner_aic: str,
        state: TaskState,
        product_text: Optional[str] = None,
        awaiting_prompt: Optional[str] = None,
    ) -> None:
        """记录任务状态"""
        self.touch()
        if task_id not in self.task_states:
            self.task_states[task_id] = {}
            logger.debug(
                "[GroupLeaderSession] Created new task entry: session=%s task_id=%s",
                self.session_id,
                task_id,
            )

        old_state = self.task_states[task_id].get(partner_aic)
        self.task_states[task_id][partner_aic] = state

        logger.info(
            "[GroupLeaderSession] Task state updated: session=%s task_id=%s partner=%s old_state=%s new_state=%s total_partners=%d",
            self.session_id,
            task_id,
            partner_aic,
            old_state,
            state,
            len(self.task_states[task_id]),
        )

        if product_text:
            if task_id not in self.task_products:
                self.task_products[task_id] = {}
            self.task_products[task_id][partner_aic] = product_text

        # 处理额外的 prompt 信息
        if awaiting_prompt is not None:
            self.task_prompts.setdefault(task_id, {})[partner_aic] = awaiting_prompt
        elif task_id in self.task_prompts:
            self.task_prompts[task_id].pop(partner_aic, None)

        # 触发状态更新事件
        self.state_update_event.set()

    def get_partner_task_snapshot(
        self, task_id: str, partner_aic: str
    ) -> Optional[Dict[str, Any]]:
        """
        获取特定Partner在指定任务中的状态快照

        包含：
        - 当前状态 (state)
        - 产出物 (product_text)
        - 等待输入的提示 (awaiting_prompt)
        """
        state = self.task_states.get(task_id, {}).get(partner_aic)
        if not state:
            return None
        return {
            "task_id": task_id,
            "state": state,
            "product_text": self.task_products.get(task_id, {}).get(partner_aic),
            "awaiting_prompt": self.task_prompts.get(task_id, {}).get(partner_aic),
        }

    def get_task_summary(self, task_id: str) -> Dict[str, Any]:
        """获取任务摘要"""
        return {
            "task_id": task_id,
            "states": self.task_states.get(task_id, {}),
            "products": self.task_products.get(task_id, {}),
            "completed_count": sum(
                1
                for s in self.task_states.get(task_id, {}).values()
                if s == TaskState.Completed
            ),
            "working_count": sum(
                1
                for s in self.task_states.get(task_id, {}).values()
                if s in (TaskState.Accepted, TaskState.Working)
            ),
        }

    def record_message(
        self, message: Union[TaskCommand, TaskResult, GroupMgmtCommand, GroupMgmtResult]
    ) -> None:
        """记录消息到历史"""
        self.touch()
        self.message_history.append(message)
        if len(self.message_history) > self.MAX_HISTORY:
            self.message_history = self.message_history[-self.MAX_HISTORY :]


class GroupLeader:
    """
    通用群组Leader实现

    提供：
    - MQ连接管理
    - 会话(Session)管理
    - 任务生命周期管理
    - 成员管理
    """

    def __init__(
        self,
        leader_aic: str,
        rabbitmq_config: Dict[str, Any],
        ssl_context: Optional[ssl.SSLContext] = None,
        ssl_cert: Optional[tuple] = None,
        invitation_timeout_seconds: int = DEFAULT_INVITATION_TIMEOUT_SECONDS,
        identity_binding_enabled: bool = True,
        *,
        message_emitter: MessageEmitter | None = None,
        trace_context_provider: Callable[[], TraceContext | None] | None = None,
    ) -> None:
        self.leader_aic = leader_aic
        self.ssl_context = ssl_context
        self.ssl_cert = ssl_cert
        self._message_emitter = message_emitter
        self._trace_context_provider = trace_context_provider
        self.rabbitmq_host = rabbitmq_config.get("host", "localhost")
        self.rabbitmq_port = rabbitmq_config.get("port", 5672)
        self.rabbitmq_vhost = rabbitmq_config.get("vhost", "/")
        self.rabbitmq_user = rabbitmq_config.get("user")
        self.rabbitmq_password = rabbitmq_config.get("password")
        self.auth_service_url = rabbitmq_config.get("auth_service_url")
        self._invitation_timeout_seconds = invitation_timeout_seconds
        self._identity_binding_enabled = identity_binding_enabled
        self._connection: Optional[AbstractConnection] = None
        self._connection_lock = asyncio.Lock()
        self._inbox_channel: Optional[AbstractChannel] = None
        self._inbox_queue: Optional[AbstractQueue] = None
        self._pending_invitations: Dict[str, PendingInvitation] = {}
        self.group_sessions: Dict[str, GroupLeaderSession] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._group_acl_client: Optional[GroupAclClient] = None
        self._force_remove_grace_period_seconds = 1.0
        self._force_remove_poll_interval_seconds = 0.05
        if isinstance(self.auth_service_url, str) and self.auth_service_url.strip():
            self._group_acl_client = GroupAclClient(
                base_url=self.auth_service_url.strip(),
                ssl_context=self.ssl_context,
            )

    async def _ensure_connection(self) -> AbstractConnection:
        async with self._connection_lock:
            if self._connection and not getattr(self._connection, "is_closed", False):
                await self._ensure_inbox()
                return self._connection

            connection_name = f"agent-{self.leader_aic}"
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

            self._connection = await aio_pika.connect_robust(
                connection_url,
                **connection_kwargs,
            )
            await self._ensure_inbox()
            logger.info(
                "event=rabbitmq_shared_connection_ready leader_aic=%s host=%s vhost=%s",
                self.leader_aic,
                self.rabbitmq_host,
                self.rabbitmq_vhost,
            )
            return self._connection

    async def _ensure_inbox(self) -> None:
        if self._inbox_queue and not getattr(self._inbox_queue, "is_closed", False):
            return
        if not self._connection:
            raise RuntimeError("RabbitMQ connection is not ready")

        inbox_name = build_inbox_queue_name(self.leader_aic)
        self._inbox_channel = await self._connection.channel()
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

        async def on_message(message: AbstractIncomingMessage) -> None:
            async with message.process():
                try:
                    body = json.loads(message.body.decode())
                except json.JSONDecodeError:
                    logger.warning(
                        "event=leader_inbox_invalid_json leader_aic=%s",
                        self.leader_aic,
                    )
                    return

                if body.get("type") != "group-invitation-error":
                    logger.debug(
                        "event=leader_inbox_ignored leader_aic=%s type=%s",
                        self.leader_aic,
                        body.get("type"),
                    )
                    return

                try:
                    error_message = InboxGroupInvitationError.model_validate(body)
                except Exception as exc:
                    logger.warning(
                        "event=leader_inbox_invalid_message leader_aic=%s error=%s",
                        self.leader_aic,
                        str(exc),
                    )
                    return

                if self._identity_binding_enabled:
                    assert_incoming_group_message_identity(
                        error_message,
                        amqp_user_id=extract_amqp_user_id(message),
                        expected_group_id=error_message.groupId,
                    )

                await self._handle_invitation_error(error_message)

        await self._inbox_queue.consume(on_message)

    def _get_session(self, session_id: Optional[str]) -> GroupLeaderSession:
        if session_id:
            session = self.group_sessions.get(session_id)
            if not session:
                raise ValueError(f"Group session not found: {session_id}")
            return session

        if len(self.group_sessions) == 1:
            return next(iter(self.group_sessions.values()))

        raise ValueError("Multiple sessions active; please specify session_id")

    # ------------------------------------------------------------------
    # 群组初始化与成员邀请
    # ------------------------------------------------------------------
    async def create_group_session(
        self, session_id: str, initial_partners: List[ACSObject]
    ) -> GroupLeaderSession:
        """
        创建群组会话

        Args:
            session_id: 会话ID
            initial_partners: 初始Partner列表
        """

        connection = await self._ensure_connection()
        group_id_seed = normalize_group_id(f"group-{session_id}")

        session_client = GroupLeaderMqClient(
            leader_aic=self.leader_aic,
            rabbitmq_host=self.rabbitmq_host,
            rabbitmq_port=self.rabbitmq_port,
            rabbitmq_vhost=self.rabbitmq_vhost,
            rabbitmq_user=self.rabbitmq_user,
            rabbitmq_password=self.rabbitmq_password,
            ssl_context=self.ssl_context,
            connection=connection,
            connection_owner=False,
            identity_binding_enabled=self._identity_binding_enabled,
            message_emitter=self._message_emitter,
            trace_context_provider=self._trace_context_provider,
        )

        await session_client.connect()

        try:
            if self._group_acl_client:
                await self._group_acl_client.add_member(
                    leader_aic=self.leader_aic,
                    group_id=group_id_seed,
                    member_aic=self.leader_aic,
                )
                for partner in initial_partners:
                    await self._group_acl_client.add_member(
                        leader_aic=self.leader_aic,
                        group_id=group_id_seed,
                        member_aic=partner.aic,
                    )

            group_id = await session_client.create_group(
                group_id=group_id_seed, partner_acs_list=initial_partners
            )
        except Exception:
            if self._group_acl_client:
                try:
                    await self._group_acl_client.delete_group(
                        leader_aic=self.leader_aic,
                        group_id=group_id_seed,
                    )
                except GroupAclError:
                    logger.warning(
                        "event=group_acl_rollback_failed leader_aic=%s group_id=%s",
                        self.leader_aic,
                        group_id_seed,
                    )
            raise

        session_client.set_message_handler(self._handle_group_message)
        try:
            await session_client.start_consuming()
        except Exception:
            # 如果消费启动失败，尝试清理由刚创建的资源，保持一致性
            try:
                if self._group_acl_client:
                    try:
                        await self._group_acl_client.delete_group(
                            leader_aic=self.leader_aic,
                            group_id=group_id,
                        )
                    except GroupAclError:
                        logger.warning(
                            "event=group_acl_delete_failed_after_start_error group_id=%s",
                            group_id,
                        )
                await session_client.dissolve_group()
            finally:
                raise

        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        session = GroupLeaderSession(
            session_id=session_id,
            group_id=group_id,
            leader_mq_client=session_client,
        )
        self.group_sessions[session_id] = session
        logger.info(
            "event=group_session_created session_id=%s group_id=%s partner_count=%d",
            session_id,
            group_id,
            len(initial_partners),
        )
        return session

    async def invite_partner(
        self,
        session_id: str,
        partner_acs: ACSObject,
        partner_rpc_url: Optional[str] = None,
        partner_acs_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        邀请Partner加入会话

        Args:
            session_id: 会话ID
            partner_acs: Partner ACS信息
            partner_rpc_url: Partner RPC地址
        """
        session = self.group_sessions.get(session_id)
        if not session:
            raise ValueError(f"Group session not found: {session_id}")

        amqp_url = get_endpoint_url(partner_acs_data, "AMQP", aic=partner_acs.aic)
        rpc_url = partner_rpc_url or get_endpoint_url(
            partner_acs_data, "JSONRPC", aic=partner_acs.aic
        )

        if amqp_url:
            amqp_endpoint = parse_amqp_endpoint_url(amqp_url, aic=partner_acs.aic)
            # Treat compose placeholder host "rabbitmq" as same broker as local advertise.
            _amqp_hosts = {self.rabbitmq_host, "rabbitmq"}
            if (
                amqp_endpoint.host in _amqp_hosts
                and amqp_endpoint.port == self.rabbitmq_port
                and amqp_endpoint.vhost == self.rabbitmq_vhost
            ):
                session.record_invitation_route(partner_acs.aic, "inbox")
                logger.info(
                    "event=partner_invite_route_selected session_id=%s partner=%s route=inbox inbox=%s",
                    session.session_id,
                    partner_acs.aic,
                    amqp_endpoint.inbox,
                )
                await self._invite_partner_via_inbox(
                    session=session,
                    partner_acs=partner_acs,
                    partner_inbox=amqp_endpoint.inbox,
                )
                return True

            logger.warning(
                "event=partner_amqp_endpoint_mismatch partner=%s endpoint=%s",
                partner_acs.aic,
                amqp_url,
            )

        if rpc_url:
            session.record_invitation_route(partner_acs.aic, "rpc")
            logger.info(
                "event=partner_invite_route_selected session_id=%s partner=%s route=rpc url=%s",
                session.session_id,
                partner_acs.aic,
                rpc_url,
            )
            await self._invite_partner_via_rpc(
                session=session,
                partner_acs=partner_acs,
                rpc_url=rpc_url,
            )
            return True

        raise PartnerInviteError(
            f"No AMQP or JSONRPC invitation endpoint for partner {partner_acs.aic}"
        )

    async def _invite_partner_via_rpc(
        self,
        *,
        session: GroupLeaderSession,
        partner_acs: ACSObject,
        rpc_url: str,
    ) -> None:
        logger.info(
            "event=partner_invite_begin session_id=%s partner=%s route=rpc url=%s",
            session.session_id,
            partner_acs.aic,
            rpc_url,
        )

        if self._group_acl_client:
            await self._group_acl_client.add_member(
                leader_aic=self.leader_aic,
                group_id=session.group_id,
                member_aic=partner_acs.aic,
            )

        try:
            await session.leader_mq_client.invite_partner(
                partner_acs=partner_acs,
                partner_rpc_url=rpc_url,
                ssl_context=self.ssl_context,
                client_cert=self.ssl_cert,
            )
        except Exception:
            if self._group_acl_client:
                try:
                    await self._group_acl_client.remove_member(
                        leader_aic=self.leader_aic,
                        group_id=session.group_id,
                        member_aic=partner_acs.aic,
                    )
                except GroupAclError:
                    logger.warning(
                        "event=partner_acl_rollback_failed session_id=%s partner=%s",
                        session.session_id,
                        partner_acs.aic,
                    )
            raise

        logger.info(
            "event=partner_invited session_id=%s partner=%s route=rpc",
            session.session_id,
            partner_acs.aic,
        )

    async def _invite_partner_via_inbox(
        self,
        *,
        session: GroupLeaderSession,
        partner_acs: ACSObject,
        partner_inbox: str,
    ) -> None:
        if self._group_acl_client is None:
            raise PartnerInviteError(
                "auth_service_url is required for inbox-based invitations"
            )

        expected_inbox = build_inbox_queue_name(partner_acs.aic)
        if partner_inbox != expected_inbox:
            raise PartnerInviteError(
                f"Partner inbox mismatch for {partner_acs.aic}: {partner_inbox}"
            )

        if partner_acs.aic in self._pending_invitations:
            raise PartnerInviteError(
                f"Partner {partner_acs.aic} already has a pending invitation"
            )

        await self._group_acl_client.add_member(
            leader_aic=self.leader_aic,
            group_id=session.group_id,
            member_aic=partner_acs.aic,
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[PartnerConnectionInfo] = loop.create_future()
        invitation_token = create_invitation_token()
        expires_at = calculate_invitation_expiry(self._invitation_timeout_seconds)
        pending = PendingInvitation(
            session_id=session.session_id,
            partner_aic=partner_acs.aic,
            invitation_token=invitation_token,
            future=future,
            timeout_task=asyncio.create_task(
                self._watch_invitation_timeout(
                    session_id=session.session_id,
                    group_id=session.group_id,
                    partner_aic=partner_acs.aic,
                    invitation_token=invitation_token,
                )
            ),
        )
        self._pending_invitations[partner_acs.aic] = pending

        try:
            await session.leader_mq_client.invite_via_inbox(
                partner_acs=partner_acs,
                partner_inbox=partner_inbox,
                invitation_token=invitation_token,
                expires_at=expires_at,
                timeout_seconds=self._invitation_timeout_seconds,
            )
            await future
            self._clear_pending_invitation(partner_acs.aic)
        except Exception:
            self._clear_pending_invitation(partner_acs.aic)
            try:
                await self._group_acl_client.remove_member(
                    leader_aic=self.leader_aic,
                    group_id=session.group_id,
                    member_aic=partner_acs.aic,
                )
            except GroupAclError:
                logger.warning(
                    "event=partner_acl_rollback_failed session_id=%s partner=%s route=inbox",
                    session.session_id,
                    partner_acs.aic,
                )
            raise

        logger.info(
            "event=partner_invited session_id=%s partner=%s route=inbox",
            session.session_id,
            partner_acs.aic,
        )

    async def _watch_invitation_timeout(
        self,
        *,
        session_id: str,
        group_id: str,
        partner_aic: str,
        invitation_token: str,
    ) -> None:
        try:
            await asyncio.sleep(self._invitation_timeout_seconds)
            pending = self._pending_invitations.get(partner_aic)
            if not pending or pending.invitation_token != invitation_token:
                return
            if pending.future.done():
                return

            try:
                if self._group_acl_client:
                    await self._group_acl_client.remove_member(
                        leader_aic=self.leader_aic,
                        group_id=group_id,
                        member_aic=partner_aic,
                    )
            except GroupAclError:
                logger.warning(
                    "event=invitation_timeout_acl_cleanup_failed session_id=%s partner=%s",
                    session_id,
                    partner_aic,
                )

            pending.future.set_exception(
                PartnerInviteError(
                    f"Invitation timed out for partner {partner_aic} in group {group_id}"
                )
            )
            logger.warning(
                "event=invitation_timeout session_id=%s partner_aic=%s group_id=%s",
                session_id,
                partner_aic,
                group_id,
            )
            self._clear_pending_invitation(partner_aic)
        except asyncio.CancelledError:
            raise

    async def _handle_invitation_error(
        self, error_message: InboxGroupInvitationError
    ) -> None:
        partner_aic = error_message.senderId or error_message.partnerAic
        pending = self._pending_invitations.get(partner_aic)
        if not pending:
            logger.warning(
                "event=invitation_error_without_pending partner=%s group_id=%s",
                partner_aic,
                error_message.groupId,
            )
            return

        if pending.invitation_token != error_message.invitationToken:
            logger.warning(
                "event=invitation_error_token_mismatch partner=%s expected=%s actual=%s",
                partner_aic,
                pending.invitation_token,
                error_message.invitationToken,
            )
            return

        if self._group_acl_client:
            try:
                await self._group_acl_client.remove_member(
                    leader_aic=self.leader_aic,
                    group_id=error_message.groupId,
                    member_aic=partner_aic,
                )
            except GroupAclError:
                logger.warning(
                    "event=invitation_error_acl_cleanup_failed partner=%s group_id=%s",
                    partner_aic,
                    error_message.groupId,
                )

        error_data = error_message.error.data
        details = getattr(error_data, "details", None)
        error_type = getattr(error_data, "errorType", None)
        if error_message.error.code == -32020 or error_type == "INVITATION_REJECTED":
            exc: PartnerInviteError = PartnerResponseError(error_message.error.message)
        else:
            detail_suffix = f": {details}" if details else ""
            exc = PartnerNetworkError(f"{error_message.error.message}{detail_suffix}")

        if not pending.future.done():
            pending.future.set_exception(exc)
        self._clear_pending_invitation(partner_aic)

    def _clear_pending_invitation(self, partner_aic: str) -> None:
        pending = self._pending_invitations.pop(partner_aic, None)
        if pending:
            pending.timeout_task.cancel()

    # ------------------------------------------------------------------
    # 任务控制
    # ------------------------------------------------------------------
    async def start_task(
        self,
        session_id: str,
        *,
        task_content: str,
        task_id: Optional[str] = None,
        target_partners: Optional[List[str]] = None,
    ) -> str:
        """启动任务"""
        session = self.group_sessions.get(session_id)
        if not session:
            raise ValueError(f"Group session not found: {session_id}")

        return await session.leader_mq_client.start_task(
            session_id=session_id,
            text_content=task_content,
            task_id=task_id,
            mentions=target_partners,
        )

    async def continue_task(
        self,
        session_id: str,
        task_id: str,
        content: str,
        target_partner: Optional[str] = None,
    ) -> None:
        """继续任务"""
        mentions = [target_partner] if target_partner else None
        session = self.group_sessions.get(session_id)
        if not session:
            raise ValueError(f"Group session not found: {session_id}")

        await session.leader_mq_client.continue_task(
            task_id=task_id,
            session_id=session_id,
            text_content=content,
            mentions=mentions,
        )

    async def complete_task(
        self,
        session_id: str,
        task_id: str,
        target_partner: Optional[str] = None,
    ) -> None:
        """完成任务"""
        mentions = [target_partner] if target_partner else None
        session = self.group_sessions.get(session_id)
        if not session:
            raise ValueError(f"Group session not found: {session_id}")

        await session.leader_mq_client.complete_task(
            task_id=task_id,
            session_id=session_id,
            mentions=mentions,
        )

    async def cancel_task(
        self,
        session_id: str,
        task_id: str,
        reason: Optional[str] = None,
        target_partner: Optional[str] = None,
    ) -> None:
        """取消任务"""
        mentions = [target_partner] if target_partner else None
        session = self.group_sessions.get(session_id)
        if not session:
            raise ValueError(f"Group session not found: {session_id}")

        await session.leader_mq_client.cancel_task(
            task_id=task_id,
            session_id=session_id,
            reason=reason,
            mentions=mentions,
        )

    # ------------------------------------------------------------------
    # 群组管理命令
    # ------------------------------------------------------------------
    async def check_partner_status(
        self, partner_aic: Optional[str] = None, session_id: Optional[str] = None
    ) -> GroupMgmtCommand:
        session = self._get_session(session_id)
        return await session.leader_mq_client.get_partner_status(
            partner_aic, session_id=session.session_id
        )

    async def request_partner_leave(
        self, partner_aic: str, session_id: Optional[str] = None
    ) -> GroupMgmtCommand:
        session = self._get_session(session_id)
        return await session.leader_mq_client.request_partner_leave(
            partner_aic, session_id=session.session_id
        )

    def get_group_runtime(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        session = self._get_session(session_id)
        members = [
            {
                "partner_aic": partner.aic,
                "invitation_route": session.invitation_routes.get(partner.aic),
                "connected": partner.connected,
                "muted": partner.muted,
                "connection_name": partner.connection_name,
                "vhost": partner.vhost,
                "node_name": partner.node_name,
                "queue_name": partner.queue_name,
                "joined_at": partner.joined_at,
            }
            for _, partner in sorted(session.leader_mq_client.partners.items())
        ]
        pending_invitations = sorted(
            partner_aic
            for partner_aic, pending in self._pending_invitations.items()
            if pending.session_id == session.session_id
        )
        connected_members = sum(1 for member in members if member["connected"])
        return {
            "session_id": session.session_id,
            "group_id": session.group_id,
            "leader_aic": self.leader_aic,
            "state": session.leader_mq_client.state.value,
            "total_members": len(members),
            "connected_members": connected_members,
            "pending_invitations": pending_invitations,
            "members": members,
        }

    async def _wait_for_partner_disconnect(
        self,
        session: GroupLeaderSession,
        partner_aic: str,
    ) -> bool:
        deadline = (
            asyncio.get_running_loop().time() + self._force_remove_grace_period_seconds
        )
        while True:
            partner = session.leader_mq_client.partners.get(partner_aic)
            if partner is not None and not partner.connected:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(self._force_remove_poll_interval_seconds)

    async def force_remove_partner(
        self, partner_aic: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        session = self._get_session(session_id)
        if self._group_acl_client is None:
            raise PartnerInviteError(
                "auth_service_url is required for force-remove operations"
            )

        logger.info(
            "event=partner_force_remove_begin session_id=%s group_id=%s partner_aic=%s",
            session.session_id,
            session.group_id,
            partner_aic,
        )

        try:
            await session.leader_mq_client.request_partner_leave(
                partner_aic,
                session_id=session.session_id,
            )
        except Exception as exc:
            logger.warning(
                "event=partner_force_remove_leave_request_failed session_id=%s group_id=%s partner_aic=%s error=%s",
                session.session_id,
                session.group_id,
                partner_aic,
                str(exc),
            )
        else:
            graceful_disconnect = await self._wait_for_partner_disconnect(
                session,
                partner_aic,
            )
            if graceful_disconnect:
                await self._group_acl_client.remove_member(
                    leader_aic=self.leader_aic,
                    group_id=session.group_id,
                    member_aic=partner_aic,
                )
                session.leader_mq_client.remove_partner_tracking(partner_aic)
                logger.info(
                    "event=partner_force_removed session_id=%s group_id=%s partner_aic=%s queue_deleted=%s mode=%s",
                    session.session_id,
                    session.group_id,
                    partner_aic,
                    False,
                    "graceful",
                )
                return {
                    "session_id": session.session_id,
                    "group_id": session.group_id,
                    "partner_aic": partner_aic,
                    "queue_deleted": False,
                }

        await self._group_acl_client.remove_member(
            leader_aic=self.leader_aic,
            group_id=session.group_id,
            member_aic=partner_aic,
        )
        queue_deleted = await session.leader_mq_client.delete_partner_queue(partner_aic)
        await self._group_acl_client.close_member_connection(
            leader_aic=self.leader_aic,
            group_id=session.group_id,
            member_aic=partner_aic,
        )

        session.leader_mq_client.remove_partner_tracking(partner_aic)
        logger.info(
            "event=partner_force_removed session_id=%s group_id=%s partner_aic=%s queue_deleted=%s mode=%s",
            session.session_id,
            session.group_id,
            partner_aic,
            queue_deleted,
            "forced",
        )
        return {
            "session_id": session.session_id,
            "group_id": session.group_id,
            "partner_aic": partner_aic,
            "queue_deleted": queue_deleted,
        }

    async def dissolve_group_session(self, session_id: str) -> None:
        session = self.group_sessions.pop(session_id, None)
        if session:
            for partner_aic, pending in list(self._pending_invitations.items()):
                if pending.session_id != session_id:
                    continue
                if not pending.future.done():
                    pending.future.set_exception(
                        PartnerInviteError(
                            f"Group session {session_id} dissolved before invitation completed"
                        )
                    )
                self._clear_pending_invitation(partner_aic)

            session.leader_mq_client._state = GroupLeaderState.DISSOLVING
            await session.leader_mq_client.broadcast_group_shutdown()
            if self._group_acl_client:
                try:
                    await self._group_acl_client.delete_group(
                        leader_aic=self.leader_aic,
                        group_id=session.group_id,
                    )
                except GroupAclError:
                    logger.warning(
                        "event=group_acl_delete_failed session_id=%s group_id=%s",
                        session_id,
                        session.group_id,
                    )

            await session.leader_mq_client.cleanup_group_resources()
            if session.leader_mq_client._channel and not getattr(
                session.leader_mq_client._channel, "is_closed", False
            ):
                await session.leader_mq_client._channel.close()
            session.leader_mq_client._partners.clear()
            session.leader_mq_client._state = GroupLeaderState.DISCONNECTED

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        for partner_aic, pending in list(self._pending_invitations.items()):
            if not pending.future.done():
                pending.future.set_exception(
                    PartnerInviteError("Leader is shutting down")
                )
            self._clear_pending_invitation(partner_aic)

        for session_id in list(self.group_sessions.keys()):
            await self.dissolve_group_session(session_id)

        if self._inbox_channel and not getattr(self._inbox_channel, "is_closed", False):
            await self._inbox_channel.close()

        if self._connection and not getattr(self._connection, "is_closed", False):
            await self._connection.close()

        if self._group_acl_client is not None:
            await self._group_acl_client.aclose()

    # ------------------------------------------------------------------
    # 会话清理机制
    # ------------------------------------------------------------------
    async def _cleanup_loop(self) -> None:
        """定期清理过期会话"""
        logger.info("event=cleanup_task_started")
        while True:
            try:
                await asyncio.sleep(300)  # 每5分钟检查一次
                await self._cleanup_expired_sessions()
                if not self.group_sessions:
                    logger.info("event=cleanup_task_stopped reason=no_active_sessions")
                    self._cleanup_task = None
                    break
            except asyncio.CancelledError:
                logger.info("event=cleanup_task_stopped")
                self._cleanup_task = None
                break
            except Exception as e:
                logger.error("event=cleanup_task_error error=%s", str(e))
                await asyncio.sleep(60)

    async def _cleanup_expired_sessions(self) -> None:
        """清理超时未活动的会话"""
        timeout_seconds = 3600  # 1小时超时
        now = datetime.now(timezone.utc)
        expired_sessions = []

        for session_id, session in self.group_sessions.items():
            if (now - session.last_modified).total_seconds() > timeout_seconds:
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            logger.info(
                "event=session_expired_cleanup session_id=%s reason=inactive_timeout",
                session_id,
            )
            await self.dissolve_group_session(session_id)

    # ------------------------------------------------------------------
    # RabbitMQ 消息回调
    # ------------------------------------------------------------------
    async def _handle_group_message(
        self, message: Union[TaskCommand, TaskResult, GroupMgmtCommand, GroupMgmtResult]
    ) -> None:
        msg_type = type(message).__name__
        sender_id = getattr(message, "senderId", "unknown")
        session_id = getattr(message, "sessionId", None)
        group_id = getattr(message, "groupId", None)

        # 优先通过 sessionId 查找 session，找不到时通过 groupId 反查
        session: Optional[GroupLeaderSession] = None
        if session_id:
            session = self.group_sessions.get(session_id)

        if not session and group_id:
            for s in self.group_sessions.values():
                if s.group_id == group_id:
                    session = s
                    break

        logger.debug(
            "[GroupLeader] >>> _handle_group_message: type=%s sender=%s session_id=%s group_id=%s routed=%s",
            msg_type,
            sender_id,
            session_id,
            group_id,
            session.session_id if session else None,
        )

        if not session:
            logger.warning(
                "[GroupLeader] Cannot route message: type=%s sender=%s session_id=%s group_id=%s",
                msg_type,
                sender_id,
                session_id,
                group_id,
            )
            return

        session.record_message(message)

        if isinstance(message, TaskResult):
            logger.info(
                "[GroupLeader] Processing TaskResult: session_id=%s sender=%s task_id=%s state=%s",
                session.session_id,
                sender_id,
                message.taskId,
                message.status.state if message.status else "no_status",
            )
            await self._handle_task_update(session, message)
        elif isinstance(message, GroupMgmtCommand):
            # 更新本地的 partner 状态（连接/静音等），并做额外日志处理
            session.leader_mq_client.handle_partner_status_update(message)
            self._handle_mgmt_command(message)
            logger.info(
                "event=mgmt_command_leader_received session_id=%s command=%s sender=%s",
                session.session_id,
                message.command,
                message.senderId,
            )
        elif isinstance(message, GroupMgmtResult):
            session.leader_mq_client.handle_partner_status_update(message)
            if message.status.connected:
                pending = self._pending_invitations.get(message.senderId)
                if pending and pending.session_id == session.session_id:
                    partner_info = session.leader_mq_client.partners.get(
                        message.senderId
                    )
                    if partner_info is None:
                        partner_info = PartnerConnectionInfo(
                            aic=message.senderId,
                            connection_name=f"agent-{message.senderId}",
                            vhost=self.rabbitmq_vhost,
                            node_name="unknown",
                            queue_name=build_group_queue_name(
                                self.leader_aic,
                                session.group_id,
                                message.senderId,
                            ),
                        )
                    if not pending.future.done():
                        pending.future.set_result(partner_info)
                    self._clear_pending_invitation(message.senderId)
            elif self._group_acl_client is not None:
                try:
                    await self._group_acl_client.remove_member(
                        leader_aic=self.leader_aic,
                        group_id=session.group_id,
                        member_aic=message.senderId,
                    )
                except GroupAclError:
                    logger.warning(
                        "event=partner_leave_acl_cleanup_failed session_id=%s partner_aic=%s group_id=%s",
                        session.session_id,
                        message.senderId,
                        session.group_id,
                    )
            logger.debug(
                "event=mgmt_result_leader_received session_id=%s sender=%s connected=%s muted=%s",
                session.session_id,
                message.senderId,
                message.status.connected,
                message.status.muted,
            )

    async def _handle_task_update(
        self, group_session: GroupLeaderSession, task: TaskResult
    ) -> None:
        sender_id = task.senderId
        if not sender_id:
            logger.warning("[GroupLeader] TaskResult has no senderId, skipping")
            return
        task_id = task.taskId
        state = task.status.state
        product_text = self._extract_text_from_products(task)
        awaiting_prompt = self._extract_text_from_status(task)

        # 调试日志：显示提取到的内容
        short_sender = sender_id[-8:] if len(sender_id) > 8 else sender_id
        logger.debug(
            "[GroupLeader] Task update details: partner=%s state=%s "
            "has_products=%s product_len=%s has_prompt=%s prompt_len=%s",
            short_sender,
            state,
            task.products is not None and len(task.products) > 0,
            len(product_text) if product_text else 0,
            awaiting_prompt is not None,
            len(awaiting_prompt) if awaiting_prompt else 0,
        )

        # 额外调试：显示 product 内容的前100字符
        if product_text:
            logger.info(
                "[GroupLeader] Product content preview for %s: %s",
                short_sender,
                product_text[:100] if len(product_text) > 100 else product_text,
            )

        logger.info(
            "[GroupLeader] Recording task state: session=%s task_id=%s partner=%s state=%s",
            group_session.session_id,
            task_id,
            sender_id,
            state,
        )

        group_session.record_task_state(
            task_id=task_id,
            partner_aic=sender_id,
            state=state,
            product_text=product_text,
            awaiting_prompt=awaiting_prompt,
        )

        logger.info(
            "[GroupLeader] Task state recorded successfully: session=%s task_id=%s partner=%s",
            group_session.session_id,
            task_id,
            sender_id,
        )

    def _handle_mgmt_command(self, message: GroupMgmtCommand) -> None:
        if message.command in (
            GroupMgmtCommandType.MUTE,
            GroupMgmtCommandType.UNMUTE,
            GroupMgmtCommandType.LEAVE_GROUP,
            GroupMgmtCommandType.DISBAND,
        ):
            logger.info(
                "event=group_mgmt_command command=%s sender=%s",
                message.command,
                message.senderId,
            )

    @staticmethod
    def _extract_text_from_products(task: TaskResult) -> Optional[str]:
        if not task.products:
            return None
        parts: List[str] = []
        for product in task.products:
            for item in product.dataItems:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts) if parts else None

    @staticmethod
    def _extract_text_from_status(task: TaskResult) -> Optional[str]:
        items = task.status.dataItems or []
        texts = [
            text
            for item in items
            if isinstance((text := getattr(item, "text", None)), str)
        ]
        return "\n".join(texts) if texts else None
