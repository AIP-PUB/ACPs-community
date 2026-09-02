"""
AIP v2 群组模式数据模型定义

本模块定义了 AIP v2 协议中群组模式相关的数据对象，包括：
- 枚举类型：GroupMgmtCommandType
- 群组管理命令：GroupMgmtCommand (继承自 Message)
- 群组管理结果：GroupMgmtResult (继承自 Message)
- RabbitMQ 相关配置
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
import uuid

from pydantic import BaseModel, Field, model_validator

from .aip_base_model import Message
from .aip_rpc_model import JSONRPCError, JSONRPCRequest, JSONRPCResponse


# =============================================================================
# ACS 对象
# =============================================================================


class ACSObject(BaseModel):
    """智能体能力说明对象

    TODO: 应该用SDK中的 AgentCapabilitySpec 来传递ACS对象。
    """

    aic: str


# =============================================================================
# 群组信息
# =============================================================================


class GroupInfo(BaseModel):
    """群组信息"""

    groupId: str
    leader: ACSObject
    partners: List[ACSObject]


# =============================================================================
# RabbitMQ 配置
# =============================================================================


class RabbitMQServerConfig(BaseModel):
    """RabbitMQ 服务器配置"""

    host: str
    port: int
    vhost: str
    accessToken: Optional[str] = None


class AMQPConfig(BaseModel):
    """AMQP 配置"""

    exchange: str
    exchangeType: str
    routingKey: str


class RabbitMQRequestParams(BaseModel):
    """RabbitMQ 请求参数"""

    protocol: str
    group: GroupInfo
    server: RabbitMQServerConfig
    amqp: AMQPConfig


class RabbitMQRequest(JSONRPCRequest):
    """RabbitMQ 群组加入请求"""

    method: Literal["group"] = "group"
    params: RabbitMQRequestParams


class RabbitMQResponseResult(BaseModel):
    """RabbitMQ 响应结果"""

    connectionName: str
    vhost: str
    nodeName: str
    queueName: str
    processId: Optional[str] = None


class RabbitMQResponseErrorData(BaseModel):
    """RabbitMQ 响应错误数据"""

    errorType: str
    details: Optional[Any] = None


class RabbitMQResponseError(JSONRPCError):
    """RabbitMQ 响应错误"""

    data: Optional[RabbitMQResponseErrorData] = None


class RabbitMQResponse(JSONRPCResponse):
    """RabbitMQ 群组加入响应"""

    result: Optional[RabbitMQResponseResult] = None
    error: Optional[RabbitMQResponseError] = None


# =============================================================================
# AIP v2 群组管理类型
# =============================================================================


class GroupMgmtCommandType(str, Enum):
    """
    AIP v2: 群组管理命令类型枚举

    注意：v1 中此枚举名为 GroupMgmtCommand，v2 重命名为 GroupMgmtCommandType，
    因为 GroupMgmtCommand 现在是一个继承自 Message 的类。
    """

    GET_STATUS = "get-status"
    LEAVE_GROUP = "leave-group"
    DISBAND = "disband"
    MUTE = "mute"
    UNMUTE = "unmute"


class GroupMemberStatus(BaseModel):
    """群组成员状态"""

    connected: bool
    muted: bool


class GroupMgmtCommand(Message):
    """
    AIP v2 群组管理命令

    继承自 Message，用于发送群组管理相关的命令。
    群组管理命令必须携带 groupId 以明确目标群组。
    """

    type: Literal["group-mgmt-command"] = "group-mgmt-command"
    command: GroupMgmtCommandType  # 必填
    groupId: str  # 必填，覆盖基类的可选声明

    @property
    def is_status_query(self) -> bool:
        """是否是状态查询命令"""
        return self.command == GroupMgmtCommandType.GET_STATUS

    @property
    def is_leave_request(self) -> bool:
        """是否是退出请求"""
        return self.command == GroupMgmtCommandType.LEAVE_GROUP

    @property
    def is_mute_command(self) -> bool:
        """是否是静音命令"""
        return self.command == GroupMgmtCommandType.MUTE

    @property
    def is_unmute_command(self) -> bool:
        """是否是取消静音命令"""
        return self.command == GroupMgmtCommandType.UNMUTE

    def is_mentioned(self, aic: str) -> bool:
        """
        检查指定的AIC是否被提及

        Args:
            aic: 要检查的智能体AIC

        Returns:
            True表示被提及（需要响应），False表示未被提及
        """
        if self.mentions is None or (
            isinstance(self.mentions, list) and len(self.mentions) == 0
        ):
            return False
        if self.mentions == "all":
            return True
        if isinstance(self.mentions, list) and aic in self.mentions:
            return True
        return False

    def must_respond(self, aic: str) -> bool:
        """
        检查指定的AIC是否必须响应

        Args:
            aic: 要检查的智能体AIC

        Returns:
            True表示必须响应，False表示可选
        """
        if self.mentions == "all":
            return True
        if isinstance(self.mentions, list) and aic in self.mentions:
            return True
        return False


class GroupMgmtResult(Message):
    """
    AIP v2 群组管理结果

    继承自 Message，用于返回群组成员的状态信息。
    群组管理结果必须携带 groupId 以明确所属群组。
    Partner 在回复时应回显收到的 GroupMgmtCommand 中的 sessionId（而非自行生成）。
    """

    type: Literal["group-mgmt-result"] = "group-mgmt-result"
    status: GroupMemberStatus  # 必填
    groupId: str  # 必填，覆盖基类的可选声明


class GroupInvitationErrorData(RabbitMQResponseErrorData):
    """Inbox 邀请错误数据。"""


class GroupInvitationError(BaseModel):
    """Inbox 邀请错误对象。"""

    code: int
    message: str
    data: Optional[GroupInvitationErrorData] = None


class InboxGroupInvitation(Message):
    """通过 inbox.topic 发送的群组邀请。"""

    type: Literal["group-invitation"] = "group-invitation"
    senderRole: Literal["leader"] = "leader"
    groupId: str
    protocol: str
    expiresAt: str
    invitationToken: str
    group: GroupInfo
    amqp: AMQPConfig

    @model_validator(mode="before")
    @classmethod
    def _fill_legacy_identity_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        group = data.get("group")
        group_id = data.get("groupId")
        if group_id is None and isinstance(group, dict):
            group_id = group.get("groupId")
        if group_id is None and isinstance(group, GroupInfo):
            group_id = group.groupId
        if group_id is not None:
            data["groupId"] = group_id
        if data.get("senderId") is None and isinstance(group, dict):
            leader = group.get("leader")
            if isinstance(leader, dict):
                leader_aic = leader.get("aic")
                if leader_aic:
                    data["senderId"] = leader_aic
        if data.get("senderId") is None and isinstance(group, GroupInfo) and group.leader:
            data["senderId"] = group.leader.aic
        data.setdefault("id", f"invite-{uuid.uuid4()}")
        data.setdefault("sentAt", datetime.now(timezone.utc).isoformat())
        data.setdefault("senderRole", "leader")
        return data

    @model_validator(mode="after")
    def _validate_identity_consistency(self) -> "InboxGroupInvitation":
        if self.groupId != self.group.groupId:
            raise ValueError("groupId must equal group.groupId")
        if self.senderId != self.group.leader.aic:
            raise ValueError("senderId must equal group.leader.aic")
        return self


class InboxGroupInvitationError(Message):
    """Partner 通过 inbox.topic 回传的邀请错误。"""

    type: Literal["group-invitation-error"] = "group-invitation-error"
    senderRole: Literal["partner"] = "partner"
    groupId: str
    partnerAic: Optional[str] = None
    invitationToken: str
    error: GroupInvitationError

    @model_validator(mode="before")
    @classmethod
    def _fill_legacy_sender_id(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if data.get("senderId") is None and data.get("partnerAic") is not None:
            data["senderId"] = data["partnerAic"]
        data.setdefault("id", f"invite-err-{uuid.uuid4()}")
        data.setdefault("sentAt", datetime.now(timezone.utc).isoformat())
        data.setdefault("senderRole", "partner")
        return data

    @model_validator(mode="after")
    def _validate_identity_consistency(self) -> "InboxGroupInvitationError":
        if self.partnerAic is not None and self.partnerAic != self.senderId:
            raise ValueError("partnerAic must equal senderId when present")
        return self
